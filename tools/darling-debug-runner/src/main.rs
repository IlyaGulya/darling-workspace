use anyhow::{Context, Result, bail};
use chrono::Utc;
use clap::{Args, Parser, Subcommand};
use nix::sys::signal::{Signal, killpg};
use nix::unistd::{Pid, getpgid};
use regex::Regex;
use std::borrow::Cow;
use std::collections::{HashMap, HashSet};
use std::fs::{self, File};
use std::io::{Read, Seek, SeekFrom};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitCode, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

#[derive(Parser)]
#[command(about = "Run and capture local or Darling debugging experiments")]
struct Cli {
    #[command(subcommand)]
    command: RunnerCommand,
}

mod diag;

#[derive(Subcommand)]
enum RunnerCommand {
    Run(RunArgs),
    Darling(DarlingArgs),
    Capture(CaptureArgs),
    Signal(SignalArgs),
    /// Resolve a reported offset to `symbol + offset` for any image (guest dylib or server binary).
    #[command(subcommand)]
    Diag(diag::DiagCommand),
}

#[derive(Args)]
struct CommonArgs {
    #[arg(long)]
    name: String,
    #[arg(long, default_value = "~/work/darling-debug")]
    bundle_root: PathBuf,
    #[arg(long)]
    cwd: Option<PathBuf>,
    #[arg(long, default_value_t = 600)]
    timeout_seconds: u64,
    #[arg(long, default_value_t = 3)]
    poll_seconds: u64,
    #[arg(long, value_parser = parse_key_value)]
    env: Vec<(String, String)>,
    #[arg(long)]
    stall_log: Option<PathBuf>,
    #[arg(long)]
    stall_pattern: Option<String>,
    #[arg(long)]
    stall_after_output: Option<String>,
    #[arg(long)]
    stall_seconds: Option<u64>,
    #[arg(long)]
    capture_command: Option<String>,
    #[arg(long)]
    prepare_command: Option<String>,
    #[arg(long)]
    capture_gdb: bool,
    #[arg(long)]
    capture_tree: bool,
    /// Also capture GDB from inside the target's PID namespace.
    #[arg(long)]
    gdb_namespace: bool,
    #[arg(long)]
    gdb_executable: Option<PathBuf>,
    #[arg(long)]
    gdb_cwd: Option<PathBuf>,
    #[arg(long)]
    gdb_ex: Vec<String>,
    #[arg(long)]
    capture_pattern: Option<String>,
    /// Record the out-of-band futex trace for THIS run into this file, with the probe the reader expects
    /// (`probes/futex.bt`, embedded in the binary). It needs root: `sudo -n bpftrace` must work without a password.
    /// The trace is what makes "who woke whom" answerable at all -- a /proc syscall snapshot is one instant.
    #[arg(long)]
    futex_trace_out: Option<PathBuf>,
    /// Capture as soon as this REGEX appears in the watch log (or in the workload's own output when
    /// no --stall-log is given), instead of waiting for silence. A workload that fails by its own
    /// timeout prints a last line and then tears itself down, and a stall detector armed after that
    /// point captures nothing: this names the marker instead.
    #[arg(long)]
    capture_on_line: Option<String>,
    /// Capture when the target's threads are in a state you name, instead of when a log goes quiet.
    /// Grammar: `state=<S|D|R>`, `wchan=<substring>`, `syscall=<number>`, `all-waiting=<seconds>`.
    /// Example: --capture-on-state 'all-waiting=20' catches "every thread is parked and has been for
    /// twenty seconds", which is what a hang looks like from outside and cannot be seen in a log.
    #[arg(long)]
    capture_on_state: Option<String>,
    #[arg(long, default_value_t = 2)]
    capture_snapshots: usize,
    #[arg(long, default_value_t = 3)]
    capture_interval_seconds: u64,
    #[arg(long)]
    cleanup_command: Option<String>,
    #[arg(long)]
    terminate_command: Option<String>,
    #[arg(long)]
    leave_running_on_stall: bool,
    /// pgrep -f pattern; as soon as a process matches, attach `strace -f` to it
    /// (writes rpctrace.log). Detached + flushed before any gdb capture.
    #[arg(long)]
    attach_strace: Option<String>,
    #[arg(long, default_value = "sendmsg,recvmsg")]
    attach_strace_expr: String,
    #[arg(long, default_value_t = 64)]
    attach_strace_str: usize,
    #[arg(long, default_value_t = 50)]
    attach_strace_poll_ms: u64,
    #[arg(last = true, required = true)]
    command: Vec<String>,
}

#[derive(Args)]
struct RunArgs {
    #[command(flatten)]
    common: CommonArgs,
}

#[derive(Args)]
struct DarlingArgs {
    #[command(flatten)]
    common: CommonArgs,
    #[arg(long, default_value = "~/work/darling-prefix/bin/darling")]
    darling: PathBuf,
    #[arg(long, default_value = "~/.darling")]
    dprefix: PathBuf,
    #[arg(long)]
    install_server: Option<PathBuf>,
}

#[derive(Args)]
struct CaptureArgs {
    #[arg(long, conflicts_with = "pattern")]
    pid: Option<u32>,
    #[arg(long, conflicts_with = "pid")]
    pattern: Option<String>,
    #[arg(long, default_value = "~/work/darling-debug")]
    bundle_root: PathBuf,
    #[arg(long, default_value = "capture")]
    name: String,
    #[arg(long)]
    gdb: bool,
    #[arg(long)]
    tree: bool,
    /// Also capture GDB from inside the target's PID namespace.
    #[arg(long)]
    gdb_namespace: bool,
    #[arg(long)]
    gdb_executable: Option<PathBuf>,
    #[arg(long)]
    gdb_cwd: Option<PathBuf>,
    #[arg(long)]
    gdb_ex: Vec<String>,
    #[arg(long, default_value_t = 2)]
    snapshots: usize,
    #[arg(long, default_value_t = 3)]
    interval_seconds: u64,
}

#[derive(Args)]
struct SignalArgs {
    #[arg(long, conflicts_with = "pattern")]
    pid: Option<u32>,
    #[arg(long, conflicts_with = "pid")]
    pattern: Option<String>,
    #[arg(long)]
    group: bool,
    #[arg(long, default_value = "TERM")]
    signal: String,
}

struct StallDetector {
    log: PathBuf,
    pattern: Regex,
    after_output: Option<Regex>,
    stall_for: Duration,
    offset: u64,
    events: u64,
    last_event: Instant,
    armed: bool,
}

impl StallDetector {
    fn from_args(args: &CommonArgs) -> Result<Option<Self>> {
        let Some(log) = &args.stall_log else {
            // Silence detection without a log to watch used to mean "no detector", so the flags were
            // accepted and did nothing. Refusing is the honest answer: the caller asked for a capture
            // on silence and would otherwise receive a run with no detector and no explanation.
            if args.stall_pattern.is_some() || args.stall_seconds.is_some() {
                bail!("--stall-pattern/--stall-seconds were given without --stall-log: silence can only be detected in a log, so name the log to watch (or drop the stall options)");
            }
            return Ok(None);
        };
        let pattern = args
            .stall_pattern
            .as_ref()
            .context("--stall-pattern is required with --stall-log")?;
        let seconds = args
            .stall_seconds
            .context("--stall-seconds is required with --stall-log")?;
        let log = expand_home(log);
        let offset = fs::metadata(&log).map_or(0, |metadata| metadata.len());
        let pattern = Regex::new(pattern).context("invalid --stall-pattern")?;
        // A pattern that matches the empty string (for example `^`) reports an event on every poll,
        // because an empty tail still matches -- so the detector can never see silence and silently
        // never fires. Refuse it instead of leaving a capture that was asked for inert.
        if pattern.is_match("") {
            bail!("--stall-pattern {pattern} matches the empty string, so it can never detect silence; use a pattern that appears only while the run is making progress");
        }
        Ok(Some(Self {
            log,
            pattern,
            after_output: args
                .stall_after_output
                .as_ref()
                .map(|value| Regex::new(value))
                .transpose()
                .context("invalid --stall-after-output")?,
            stall_for: Duration::from_secs(seconds),
            offset,
            events: 0,
            last_event: Instant::now(),
            armed: args.stall_after_output.is_none(),
        }))
    }

    fn poll(&mut self, stdout: &Path, stderr: &Path) -> Result<bool> {
        if !self.armed {
            let mut output = fs::read_to_string(stdout).unwrap_or_default();
            output.push_str(&fs::read_to_string(stderr).unwrap_or_default());
            self.armed = match &self.after_output {
                // No marker named: watch from the start. Requiring one made --stall-pattern and
                // --stall-seconds inert unless --stall-after-output was also given, so a capture
                // that was asked for produced an empty bundle and said nothing about why.
                None => true,
                Some(pattern) => pattern.is_match(&output),
            };
            if self.armed {
                self.last_event = Instant::now();
            }
        }

        if let Ok(mut file) = File::open(&self.log) {
            let len = file.metadata()?.len();
            if len < self.offset {
                self.offset = 0;
            }
            file.seek(SeekFrom::Start(self.offset))?;
            let mut new_text = String::new();
            file.read_to_string(&mut new_text)?;
            self.offset = file.stream_position()?;
            // Only appended text can be progress: counting matches in an empty tail made `^` refresh
            // the timer every poll (see from_args), which is how a real stall hid from the detector.
            let new_events = if new_text.is_empty() {
                0
            } else {
                self.pattern.find_iter(&new_text).count() as u64
            };
            if new_events > 0 {
                self.events += new_events;
                self.last_event = Instant::now();
            }
        }

        Ok(self.armed && self.last_event.elapsed() >= self.stall_for)
    }
}

fn parse_key_value(value: &str) -> Result<(String, String), String> {
    value
        .split_once('=')
        .map(|(key, value)| (key.to_owned(), value.to_owned()))
        .ok_or_else(|| "expected KEY=VALUE".to_owned())
}


/// Read the bytes a file gained since `offset` and report whether the pattern matches them. Used by
/// the marker trigger so both output streams are watched without duplicating the seek bookkeeping.
fn marker_seen(path: &Path, pattern: &Regex, offset: &mut u64) -> Result<bool> {
    let mut file = match File::open(path) {
        Ok(f) => f,
        Err(_) => return Ok(false),
    };
    let len = file.metadata().map(|m| m.len()).unwrap_or(0);
    if len < *offset {
        *offset = 0;
    }
    let _ = file.seek(SeekFrom::Start(*offset));
    let mut text = String::new();
    if file.read_to_string(&mut text).is_err() {
        return Ok(false);
    }
    *offset = file.stream_position().unwrap_or(*offset);
    Ok(pattern.is_match(&text))
}

fn expand_home(path: &Path) -> PathBuf {
    let text = path.to_string_lossy();
    if (text == "~" || text.starts_with("~/"))
        && let Some(home) = std::env::var_os("HOME")
    {
        return PathBuf::from(home).join(text.trim_start_matches("~/"));
    }
    path.to_owned()
}

fn make_bundle(root: &Path, name: &str) -> Result<PathBuf> {
    let safe_name: String = name
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || "._-".contains(c) {
                c
            } else {
                '_'
            }
        })
        .collect();
    let bundle = expand_home(root).join(format!(
        "{}-{safe_name}",
        Utc::now().format("%Y%m%dT%H%M%SZ")
    ));
    fs::create_dir_all(&bundle)?;
    Ok(bundle)
}

fn shell_hook(command: &str, bundle: &Path, pid: u32) {
    let _ = Command::new("/bin/bash")
        .args(["-lc", command])
        .env("BUNDLE", bundle)
        .env("TARGET_PID", pid.to_string())
        .status();
}

fn find_pid(pid: Option<u32>, pattern: Option<&str>) -> Result<u32> {
    if let Some(pid) = pid {
        return Ok(pid);
    }
    let pattern = pattern.context("provide --pid or --pattern")?;
    let output = Command::new("pgrep")
        .args(["-f", pattern])
        .output()
        .context("failed to run pgrep")?;
    let self_pid = std::process::id();
    let text = String::from_utf8_lossy(&output.stdout);
    text.lines()
        .filter_map(|line| line.trim().parse::<u32>().ok())
        .filter(|candidate| *candidate != self_pid)
        .filter_map(|candidate| {
            fs::read(format!("/proc/{candidate}/cmdline"))
                .ok()
                .map(|cmdline| (candidate, cmdline.len()))
        })
        .min_by_key(|(_, command_length)| *command_length)
        .map(|(candidate, _)| candidate)
        .with_context(|| format!("no process matched {pattern:?}"))
}

fn find_pid_in_tree(root: u32, pattern: &str) -> Result<u32> {
    let pattern = Regex::new(pattern).context("invalid process pattern")?;
    process_tree(root)
        .into_iter()
        .filter_map(|pid| {
            fs::read(format!("/proc/{pid}/cmdline"))
                .ok()
                .map(|cmdline| (pid, cmdline))
        })
        .filter(|(_, cmdline)| {
            pattern.is_match(&String::from_utf8_lossy(cmdline).replace('\0', " "))
        })
        .min_by_key(|(_, cmdline)| cmdline.len())
        .map(|(pid, _)| pid)
        .with_context(|| format!("no process in tree rooted at {root} matched {pattern:?}"))
}

fn command_to_file(mut command: Command, output: &Path) {
    if let Ok(file) = File::create(output) {
        let _ = command
            .stdout(file.try_clone().unwrap())
            .stderr(file)
            .status();
    }
}

fn process_tree(root: u32) -> Vec<u32> {
    let mut children: HashMap<u32, Vec<u32>> = HashMap::new();
    if let Ok(entries) = fs::read_dir("/proc") {
        for entry in entries.flatten() {
            let Ok(pid) = entry.file_name().to_string_lossy().parse::<u32>() else {
                continue;
            };
            let Ok(status) = fs::read_to_string(entry.path().join("status")) else {
                continue;
            };
            let Some(ppid) = status
                .lines()
                .find_map(|line| line.strip_prefix("PPid:"))
                .and_then(|value| value.trim().parse::<u32>().ok())
            else {
                continue;
            };
            children.entry(ppid).or_default().push(pid);
        }
    }

    let mut result = Vec::new();
    let mut seen = HashSet::new();
    let mut pending = vec![root];
    while let Some(pid) = pending.pop() {
        if !seen.insert(pid) {
            continue;
        }
        result.push(pid);
        if let Some(descendants) = children.get(&pid) {
            pending.extend(descendants);
        }
    }
    result
}

#[allow(clippy::too_many_arguments)]
fn capture_process_tree(
    root: u32,
    bundle: &Path,
    gdb: bool,
    gdb_namespace: bool,
    gdb_executable: Option<&Path>,
    gdb_cwd: Option<&Path>,
    gdb_ex: &[String],
    snapshots: usize,
    interval: Duration,
) -> Result<()> {
    let pids = process_tree(root);
    fs::create_dir_all(bundle)?;
    fs::write(
        bundle.join("tree-pids.txt"),
        pids.iter()
            .map(u32::to_string)
            .collect::<Vec<_>>()
            .join("\n")
            + "\n",
    )?;

    let pid_list = pids
        .iter()
        .map(u32::to_string)
        .collect::<Vec<_>>()
        .join(",");
    let mut ps = Command::new("ps");
    ps.args([
        "-p",
        &pid_list,
        "-o",
        "pid,ppid,pgid,sid,stat,etime,wchan:32,comm,args",
        "--forest",
    ]);
    command_to_file(ps, &bundle.join("ps-tree.txt"));

    for pid in pids {
        capture_target(
            pid,
            &bundle.join(format!("pid-{pid}")),
            gdb,
            gdb_namespace,
            gdb_executable,
            gdb_cwd,
            gdb_ex,
            snapshots,
            interval,
        )?;
    }
    Ok(())
}

#[allow(clippy::too_many_arguments)]
fn capture_target(
    pid: u32,
    bundle: &Path,
    gdb: bool,
    gdb_namespace: bool,
    gdb_executable: Option<&Path>,
    gdb_cwd: Option<&Path>,
    gdb_ex: &[String],
    snapshots: usize,
    interval: Duration,
) -> Result<()> {
    fs::create_dir_all(bundle)?;
    fs::write(bundle.join("target-pid.txt"), format!("{pid}\n"))?;

    let mut ps = Command::new("ps");
    ps.args([
        "-p",
        &pid.to_string(),
        "-Lo",
        "pid,ppid,pgid,sid,tid,stat,etime,wchan:32,comm,args",
    ]);
    command_to_file(ps, &bundle.join("ps-target.txt"));

    for name in [
        "status",
        "cmdline",
        "wchan",
        "syscall",
        "stack",
        "maps",
        "mountinfo",
    ] {
        let source = PathBuf::from(format!("/proc/{pid}/{name}"));
        let destination = bundle.join(format!("proc-{name}.txt"));
        if fs::copy(&source, &destination).is_err() {
            let mut sudo = Command::new("sudo");
            sudo.args(["-n", "cat"]).arg(source);
            command_to_file(sudo, &destination);
        }
    }

    for index in 1..=snapshots {
        for task in fs::read_dir(format!("/proc/{pid}/task"))
            .into_iter()
            .flatten()
            .flatten()
        {
            let tid = task.file_name().to_string_lossy().into_owned();
            for name in ["comm", "wchan", "stack", "syscall"] {
                let source = task.path().join(name);
                let destination = bundle.join(format!("snapshot-{index}-tid-{tid}-{name}.txt"));
                if fs::copy(&source, &destination).is_err() {
                    let mut sudo = Command::new("sudo");
                    sudo.args(["-n", "cat"]).arg(source);
                    command_to_file(sudo, &destination);
                }
            }
        }
        if gdb {
            let command = gdb_command(pid, None, gdb_executable, gdb_cwd, gdb_ex);
            command_to_file(command, &bundle.join(format!("gdb-{index}.txt")));

            if gdb_namespace {
                let namespace_pid = namespace_pid(pid).unwrap_or(pid);
                let command =
                    gdb_command(namespace_pid, Some(pid), gdb_executable, gdb_cwd, gdb_ex);
                command_to_file(command, &bundle.join(format!("gdb-namespace-{index}.txt")));
            }
        }
        if index < snapshots {
            thread::sleep(interval);
        }
    }
    Ok(())
}

fn namespace_pid(pid: u32) -> Option<u32> {
    fs::read_to_string(format!("/proc/{pid}/status"))
        .ok()?
        .lines()
        .find_map(|line| line.strip_prefix("NSpid:"))?
        .split_whitespace()
        .next_back()?
        .parse()
        .ok()
}

fn gdb_command(
    pid: u32,
    namespace_target: Option<u32>,
    executable: Option<&Path>,
    cwd: Option<&Path>,
    expressions: &[String],
) -> Command {
    let mut command = Command::new("timeout");
    command.args(["30s", "sudo", "-n"]);
    if let Some(target) = namespace_target {
        command
            .args([
                "nsenter",
                "--target",
                &target.to_string(),
                "--pid",
                "--mount",
                "--",
            ])
            .args(["gdb", "-q"]);
    } else {
        command.args(["gdb", "-q"]);
    }
    if let Some(cwd) = cwd {
        command.current_dir(expand_home(cwd));
    }
    if let Some(executable) = executable {
        command.arg(expand_home(executable));
    }
    command.args(["-p", &pid.to_string(), "-batch"]).args([
        "-ex",
        "set pagination off",
        "-ex",
        "info threads",
        "-ex",
        "thread apply all bt full",
    ]);
    for expression in expressions {
        command.args(["-ex", expression]);
    }
    command
}

fn terminate_group(child: &mut Child) -> Result<String> {
    let pgid = Pid::from_raw(child.id() as i32);
    let pids = process_tree(child.id());
    let mut lines = Vec::new();

    match killpg(pgid, Signal::SIGTERM) {
        Ok(()) => lines.push("sent SIGTERM to process group".to_owned()),
        Err(error) => lines.push(format!("SIGTERM failed: {error}")),
    }
    for pid in &pids {
        if *pid != std::process::id() {
            let _ = nix::sys::signal::kill(Pid::from_raw(*pid as i32), Signal::SIGTERM);
        }
    }

    let deadline = Instant::now() + Duration::from_secs(5);
    while Instant::now() < deadline {
        if let Some(status) = child.try_wait()? {
            lines.push(format!("child exited after SIGTERM: {status}"));
            return Ok(lines.join("\n") + "\n");
        }
        thread::sleep(Duration::from_millis(100));
    }

    match killpg(pgid, Signal::SIGKILL) {
        Ok(()) => lines.push("sent SIGKILL to process group".to_owned()),
        Err(error) => lines.push(format!("SIGKILL failed: {error}")),
    }
    for pid in &pids {
        if *pid != std::process::id() {
            let _ = nix::sys::signal::kill(Pid::from_raw(*pid as i32), Signal::SIGKILL);
        }
    }

    let deadline = Instant::now() + Duration::from_secs(2);
    while Instant::now() < deadline {
        if let Some(status) = child.try_wait()? {
            lines.push(format!("child exited after SIGKILL: {status}"));
            return Ok(lines.join("\n") + "\n");
        }
        thread::sleep(Duration::from_millis(50));
    }
    let remaining: Vec<u32> = pids
        .into_iter()
        .filter(|pid| Path::new(&format!("/proc/{pid}")).exists())
        .collect();
    if remaining.is_empty() {
        lines.push("process tree gone after SIGKILL; child was not reaped yet".to_owned());
    } else {
        lines.push(format!(
            "processes still alive after SIGKILL: {remaining:?}"
        ));
    }
    Ok(lines.join("\n") + "\n")
}

/// The probe program, embedded so the binary always carries the one its own reader expects. The file is
/// `probes/futex.bt`; keeping it as `include_str!` means a run cannot use a stale copy from somewhere else.
const FUTEX_PROBE_TEXT: &str = include_str!("../probes/futex.bt");

/// The out-of-band futex probe, running for the duration of the workload.
///
/// WHY A PROBE AND NOT A SNAPSHOT: a /proc syscall snapshot is one instant, so it can never show who woke whom.
/// The program is `probes/futex.bt` embedded in the binary, so the tool always carries the probe its own reader
/// expects, and its output is exactly the schema `dwdiag futex --futex-trace` parses.
struct FutexProbe {
    child: Option<Child>,
    path: PathBuf,
}

impl FutexProbe {
    fn spawn(path: &Path) -> Result<Self> {
        let path = expand_home(path);
        let file = File::create(&path).with_context(|| format!("cannot create {}", path.display()))?;
        // bpftrace diagnostics go to their own file rather than a pipe: a pipe nobody reads fills up and blocks the
        // probe, which would look exactly like a run with no futex activity.
        let err_path = path.with_extension("stderr");
        let err = File::create(&err_path).ok();
        let mut command = Command::new("sudo");
        command.args(["-n", "bpftrace", "-q", "-e", FUTEX_PROBE_TEXT]);
        command.stdout(Stdio::from(file));
        command.stderr(match err {
            Some(handle) => Stdio::from(handle),
            None => Stdio::null(),
        });
        let child = command
            .spawn()
            .context("cannot start bpftrace: --futex-trace-out needs `sudo -n bpftrace` to work")?;
        Ok(Self {
            child: Some(child),
            path,
        })
    }

    /// Stop the probe so it flushes, and say how much it recorded.
    ///
    /// SIGINT, not SIGKILL: bpftrace flushes its output on a normal signal, and a trace that silently ends early is
    /// the very failure this path exists to remove. The same reason it runs on Drop -- a run that fails early must
    /// not leave a root-owned probe behind.
    fn stop(&mut self) {
        let Some(mut child) = self.child.take() else {
            return;
        };
        let _ = nix::sys::signal::kill(Pid::from_raw(child.id() as i32), Signal::SIGINT);
        let _ = child.wait();
        let lines = fs::read_to_string(&self.path)
            .map(|text| text.lines().count())
            .unwrap_or(0);
        println!("FUTEX-TRACE-OUT {} lines={lines}", self.path.display());
    }
}

#[cfg(unix)]
impl Drop for FutexProbe {
    fn drop(&mut self) {
        self.stop();
    }
}

struct StraceAttacher {
    stop: Arc<AtomicBool>,
    child: Arc<Mutex<Option<Child>>>,
    handle: thread::JoinHandle<()>,
}

impl StraceAttacher {
    /// Poll for the earliest process matching `pattern` and attach `strace -f`
    /// to it, logging the configured syscalls to `bundle/rpctrace.log`.
    fn spawn(
        root_pid: u32,
        pattern: String,
        expr: String,
        str_len: usize,
        poll: Duration,
        deadline: Instant,
        bundle: PathBuf,
    ) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let child: Arc<Mutex<Option<Child>>> = Arc::new(Mutex::new(None));
        let stop_t = stop.clone();
        let child_t = child.clone();
        let handle = thread::spawn(move || {
            let log = bundle.join("rpctrace.log");
            let status = bundle.join("attach-strace.txt");
            while !stop_t.load(Ordering::Relaxed) && Instant::now() < deadline {
                let found = find_pid_in_tree(root_pid, &pattern).ok();
                if let Some(pid) = found {
                    let mut command = Command::new("sudo");
                    command
                        .args([
                            "-n",
                            "strace",
                            "-f",
                            "-ttt",
                            "-yy",
                            "-e",
                            &format!("trace={expr}"),
                            "-s",
                            &str_len.to_string(),
                            "-p",
                            &pid.to_string(),
                            "-o",
                        ])
                        .arg(&log);
                    match command.spawn() {
                        Ok(handle) => {
                            let _ = fs::write(
                                &status,
                                format!("attached target_pid={pid} strace_pid={}\n", handle.id()),
                            );
                            *child_t.lock().unwrap() = Some(handle);
                        }
                        Err(error) => {
                            let _ = fs::write(&status, format!("strace spawn failed: {error}\n"));
                        }
                    }
                    return;
                }
                thread::sleep(poll);
            }
            let _ = fs::write(&status, "target never appeared before deadline\n");
        });
        Self {
            stop,
            child,
            handle,
        }
    }

    /// Stop polling and detach strace via SIGINT so it flushes its log cleanly.
    /// Must run before any gdb capture (a process can only have one tracer).
    fn detach(self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(mut child) = self.child.lock().unwrap().take() {
            let _ = nix::sys::signal::kill(Pid::from_raw(child.id() as i32), Signal::SIGINT);
            let _ = child.wait();
        }
        let _ = self.handle.join();
    }
}

fn spawn(
    args: &CommonArgs,
    bundle: &Path,
    command: &[String],
    extra_env: &HashMap<String, String>,
) -> Result<Child> {
    let program = command.first().context("missing command")?;
    let stdout = File::create(bundle.join("stdout.log"))?;
    let stderr = File::create(bundle.join("stderr.log"))?;
    let mut process = Command::new(program);
    process
        .args(&command[1..])
        .envs(args.env.iter().cloned())
        .envs(extra_env)
        .stdout(stdout)
        .stderr(stderr);
    if let Some(cwd) = &args.cwd {
        process.current_dir(expand_home(cwd));
    }
    // SAFETY: setsid is async-signal-safe and does not access parent memory.
    unsafe {
        process.pre_exec(|| {
            nix::unistd::setsid().map_err(std::io::Error::other)?;
            Ok(())
        });
    }
    process.spawn().context("failed to spawn command")
}

fn run_experiment(
    args: &CommonArgs,
    command: Vec<String>,
    extra_env: HashMap<String, String>,
) -> Result<(PathBuf, &'static str)> {
    let bundle = make_bundle(&args.bundle_root, &args.name)?;
    fs::write(
        bundle.join("command.txt"),
        format!("{}\n", command.join(" ")),
    )?;
    let mut env_lines: Vec<String> = std::env::vars()
        .chain(args.env.iter().cloned())
        .chain(
            extra_env
                .iter()
                .map(|(key, value)| (key.clone(), value.clone())),
        )
        .map(|(key, value)| format!("{key}={value}"))
        .collect();
    env_lines.sort();
    fs::write(bundle.join("env.txt"), env_lines.join("\n") + "\n")?;
    if let Some(command) = &args.prepare_command {
        shell_hook(command, &bundle, 0);
    }
    // Validate the observation plan before anything is spawned or prepared: an invalid plan that is
    // discovered afterwards leaves a payload running with no way to explain what was asked for.
    if args.capture_snapshots == 0 {
        bail!("--capture-snapshots 0 asks for no snapshots, which silently skips all thread and debugger evidence; use at least 1");
    }
    if !args.capture_gdb
        && (args.gdb_namespace
            || args.gdb_executable.is_some()
            || args.gdb_cwd.is_some()
            || !args.gdb_ex.is_empty())
    {
        bail!("--gdb-namespace/--gdb-executable/--gdb-cwd/--gdb-ex were given without --capture-gdb, so they would be ignored; add --capture-gdb or drop them");
    }
    if args.capture_on_line.is_some() && args.stall_log.is_none() {
        println!("NOTE --capture-on-line watches the workload's stdout and stderr (no --stall-log named)");
    }
    println!("BUNDLE={}", bundle.display());
    let mut detector = StallDetector::from_args(args)?;
    let mut futex_probe = match args.futex_trace_out.as_ref() {
        Some(path) => Some(FutexProbe::spawn(path)?),
        None => None,
    };
    let mut child = spawn(args, &bundle, &command, &extra_env)?;
    fs::write(bundle.join("pid.txt"), format!("{}\n", child.id()))?;
    // The run window, in the SAME format the job owner writes and the readers already parse. Without it a trace
    // cannot be attributed to this run at all: `dwdiag futex --futex-trace` refuses an unattributable trace on
    // purpose, because a trace from another run reads exactly like this one.
    let run_start = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    fs::write(
        bundle.join("evidence-manifest"),
        format!("version=1\nstart={run_start}\n"),
    )?;
    let stdout = bundle.join("stdout.log");
    let stderr = bundle.join("stderr.log");
    let deadline = Instant::now() + Duration::from_secs(args.timeout_seconds);
    let strace = args.attach_strace.as_ref().map(|pattern| {
        StraceAttacher::spawn(
            child.id(),
            pattern.clone(),
            args.attach_strace_expr.clone(),
            args.attach_strace_str,
            Duration::from_millis(args.attach_strace_poll_ms),
            deadline,
            bundle.clone(),
        )
    });
    let result;
    let marker_pattern = match &args.capture_on_line {
        Some(p) => Some(Regex::new(p).context("invalid --capture-on-line")?),
        None => None,
    };
    let marker_log = args.stall_log.clone();
    let mut marker_offset: u64 = 0;
    let state_expr = match &args.capture_on_state {
        Some(e) => Some(StateTrigger::parse(e)?),
        None => None,
    };
    let mut state_quiet_since: Option<Instant> = None;
    // The interesting process is usually NOT there when the run starts: the linker is spawned by a
    // shell inside the guest, so its host process (whose cmdline carries the guest path) appears only
    // later. A pattern resolved once at startup therefore matched nothing and the trigger silently
    // watched the wrapping shell instead -- a real capture fired on the wrong process and looked
    // plausible. Resolve per poll, and say when the pattern matches nothing.
    let mut target_note: Option<String> = None;

    loop {
        if let Some(status) = child.try_wait()? {
            fs::write(bundle.join("exit-status.txt"), format!("{status}\n"))?;
            result = if status.success() { "exited" } else { "failed" };
            break;
        }
        if Instant::now() >= deadline {
            fs::write(bundle.join("timeout.txt"), "hard timeout\n")?;
            result = "timeout";
            break;
        }
        let state_target = match args.capture_pattern.as_deref() {
            Some(pattern) => match find_pid_in_tree(child.id(), pattern) {
                Ok(pid) => {
                    target_note = None;
                    pid
                }
                Err(_) => {
                    target_note = Some(format!(
                        "capture-pattern {pattern:?} matched no process in the tree; watching pid {} instead",
                        child.id()
                    ));
                    child.id()
                }
            },
            None => child.id(),
        };
        if let Some(expr) = &state_expr {
            // STATE IS A BETTER TRIGGER THAN TEXT for a hang: a workload that stops working may print
            // nothing at all, and its state is readable from /proc without touching the measured path.
            // The predicate is named by the caller (see StateTrigger), and when it fires the matching
            // threads are written out, so the capture is not a guess about why it fired.
            if let Some(threads) = expr.matches(state_target) {
                let mut report = format!("trigger={}\ntarget_pid={state_target}\n", expr.describe());
                if let Some(note) = &target_note {
                    report.push_str(&format!("note={note}\n"));
                }
                for t in &threads {
                    report.push_str(&format!(
                        "pid={} tid={} state={} wchan={} syscall={}\n",
                        t.pid, t.tid, t.state, t.wchan, t.syscall
                    ));
                }
                fs::write(bundle.join("state.txt"), report)?;
                result = "state";
                break;
            }
            match state_quiet_since {
                Some(_) => {}
                None => state_quiet_since = Some(Instant::now()),
            }
        }
        if let Some(pattern) = &marker_pattern {
            // Both streams matter: a marker a workload writes to stderr is as much a fact as one on
            // stdout, and watching only stdout lost it. An explicitly named log still wins.
            if let Some(watch) = marker_log.as_ref() {
                if let Ok(mut file) = File::open(watch) {
                    let len = file.metadata().map(|m| m.len()).unwrap_or(0);
                    if len < marker_offset {
                        marker_offset = 0;
                    }
                    let _ = file.seek(SeekFrom::Start(marker_offset));
                    let mut text = String::new();
                    if file.read_to_string(&mut text).is_ok() {
                        marker_offset = file.stream_position().unwrap_or(marker_offset);
                        if pattern.is_match(&text) {
                            fs::write(bundle.join("marker.txt"), "capture requested by --capture-on-line\n")?;
                            result = "marker";
                            break;
                        }
                    }
                }
            } else {
                let mut fired = false;
                for watch in [&stdout, &stderr] {
                    if marker_seen(watch, pattern, &mut marker_offset)? {
                        fired = true;
                        break;
                    }
                }
                if fired {
                    fs::write(bundle.join("marker.txt"), "capture requested by --capture-on-line\n")?;
                    result = "marker";
                    break;
                }
            }
        }
        if detector
            .as_mut()
            .is_some_and(|value| value.poll(&stdout, &stderr).unwrap_or(false))
        {
            let events = detector.as_ref().map_or(0, |value| value.events);
            fs::write(bundle.join("stall.txt"), format!("events={events}\n"))?;
            result = "stall";
            break;
        }
        thread::sleep(Duration::from_secs(args.poll_seconds));
    }

    // The workload is over, so the probe is stopped first: it flushes on SIGINT and reports how much it recorded.
    if let Some(probe) = futex_probe.as_mut() {
        probe.stop();
    }
    // Close the run window. It is written here rather than at startup because the end is only known now, and a
    // window without an end can only be checked from its start.
    let run_end = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let manifest_path = bundle.join("evidence-manifest");
    if let Ok(text) = fs::read_to_string(&manifest_path) {
        let _ = fs::write(
            &manifest_path,
            format!("{text}end={run_end}\n"),
        );
    }

    // Detach strace (flushing rpctrace.log) before any gdb capture: a process
    // can only have a single tracer at a time.
    if let Some(strace) = strace {
        strace.detach();
    }

    if !matches!(result, "exited" | "failed") {
        if let Some(stall_log) = &args.stall_log {
            let stall_log = expand_home(stall_log);
            let _ = fs::copy(stall_log, bundle.join("stall-log.txt"));
        }
        let _ = capture_process_tree(
            child.id(),
            &bundle.join("guarded-tree"),
            false,
            false,
            None,
            None,
            &[],
            1,
            Duration::from_secs(0),
        );
        // --capture-tree used to do NOTHING unless --capture-gdb was also given: a capture that was
        // asked for produced no directory and said nothing about why. The /proc tree is exactly what
        // a hang needs (per-thread wchan/syscall/stack at the moment it stops), and it does not need
        // the debugger, so the tree is captured whenever it is asked for, with gdb only when asked.
        if args.capture_tree || args.capture_gdb {
            let capture_pid = args
                .capture_pattern
                .as_deref()
                .and_then(|pattern| find_pid_in_tree(child.id(), pattern).ok())
                .unwrap_or(child.id());
            if args.capture_tree {
                capture_process_tree(
                    capture_pid,
                    &bundle.join("capture-tree"),
                    args.capture_gdb,
                    args.gdb_namespace,
                    args.gdb_executable.as_deref(),
                    args.gdb_cwd.as_deref(),
                    &args.gdb_ex,
                    args.capture_snapshots,
                    Duration::from_secs(args.capture_interval_seconds),
                )?;
            } else {
                capture_target(
                    capture_pid,
                    &bundle.join("capture"),
                    true,
                    args.gdb_namespace,
                    args.gdb_executable.as_deref(),
                    args.gdb_cwd.as_deref(),
                    &args.gdb_ex,
                    args.capture_snapshots,
                    Duration::from_secs(args.capture_interval_seconds),
                )?;
            }
        }
        if let Some(command) = &args.capture_command {
            shell_hook(command, &bundle, child.id());
        }
        if result != "stall" || !args.leave_running_on_stall {
            if let Some(command) = &args.terminate_command {
                shell_hook(command, &bundle, child.id());
                fs::write(bundle.join("cleanup-status.txt"), "ran terminate-command\n")?;
            } else {
                fs::write(
                    bundle.join("cleanup-status.txt"),
                    terminate_group(&mut child)?,
                )?;
            }
        }
    }
    if (result != "stall" || !args.leave_running_on_stall)
        && let Some(command) = &args.cleanup_command
    {
        shell_hook(command, &bundle, child.id());
    }
    Ok((bundle, result))
}

fn darling_command(args: &DarlingArgs) -> Result<(Vec<String>, HashMap<String, String>)> {
    let darling = expand_home(&args.darling);
    let dprefix = expand_home(&args.dprefix);
    let mut env = HashMap::new();
    env.insert("DPREFIX".to_owned(), dprefix.display().to_string());
    let mut command = vec![
        darling.display().to_string(),
        "shell".to_owned(),
        "/bin/bash".to_owned(),
        "-lc".to_owned(),
    ];
    command.push(
        args.common
            .command
            .iter()
            .map(|argument| shell_escape::escape(Cow::Borrowed(argument.as_str())).into_owned())
            .collect::<Vec<_>>()
            .join(" "),
    );
    Ok((command, env))
}

fn prepare_darling(args: &DarlingArgs) -> Result<()> {
    let darling = expand_home(&args.darling);
    let dprefix = expand_home(&args.dprefix);
    let _ = Command::new(&darling)
        .env("DPREFIX", &dprefix)
        .arg("shutdown")
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
    thread::sleep(Duration::from_secs(3));
    if let Some(server) = &args.install_server {
        let destination = darling
            .parent()
            .context("invalid --darling path")?
            .join("darlingserver");
        fs::copy(expand_home(server), destination).context("failed to install darlingserver")?;
    }
    Ok(())
}

/// The stream the CALLER is actually watching.
///
/// A long run duplicates stdout/stderr into a transcript so its output cannot be lost; but then the caller sees NOTHING,
/// which is its own defect: MEASURED, `dwdiag prefix`/`verdict` output was grepped from stdout and matched nothing while
/// the answer sat in the transcript, twice leading to "the tool was silent" conclusions that were wrong. Essentials --
/// stage lines, verdict rows, denial lines, hash checks, the final rc -- are therefore written HERE as well.
static ORIGINAL_STDOUT: std::sync::OnceLock<i32> = std::sync::OnceLock::new();

pub fn say(message: &str) {
    use std::io::Write;
    use std::os::fd::FromRawFd;
    if let Some(fd) = ORIGINAL_STDOUT.get() {
        let mut f = unsafe { std::fs::File::from_raw_fd(*fd) };
        let _ = writeln!(f, "{message}");
        let _ = f.flush();
        std::mem::forget(f);
    } else {
        println!("{message}");
    }
}

/// Print the part of a long run that a caller actually asks for: stage lines, hash checks, verdict rows, denial lines,
/// the suite table and the final verdict. The transcript next to it still holds EVERYTHING.
fn echo_essentials(path: &std::path::Path) {
    const KEEP: &[&str] = &[
        "PREFIX-STAGE", "PREFIX-INSTALL", "PREFIX-INSTALLED", "PREFIX-ENV",
        "VERDICT", "LOG=", "DENIAL", "MARKER", "SUITE", "MATCH", "MISMATCH",
        "COURIER", "WAKES", "PROGRESS", "ERROR", "error:", "Failed", "failed",
    ];
    let Ok(text) = std::fs::read_to_string(path) else { return };
    let mut printed = 0usize;
    for line in text.lines() {
        if KEEP.iter().any(|k| line.contains(k)) {
            say(line);
            printed += 1;
        }
    }
    say(&format!("ESSENTIALS {printed} line(s) from {}", path.display()));
}

fn main() -> Result<ExitCode> {
    // MEASURED defect: `dwdiag progress ... | head -4` panicked with `failed printing to stdout: Broken pipe (os error
    // 32)`, because Rust starts every process with SIGPIPE ignored. A diagnostic whose output is paged or truncated is
    // ordinary use, so restore the default disposition and let the kernel terminate the process the way any other CLI
    // would, instead of reporting a panic that says nothing about the transport under test.
    unsafe {
        nix::sys::signal::signal(
            nix::sys::signal::Signal::SIGPIPE,
            nix::sys::signal::SigHandler::SigDfl,
        )
        .expect("failed to restore the default SIGPIPE disposition");
    }
    // EVERYTHING THIS PROCESS PRINTS ALSO GOES TO A FILE, announced first.
    //
    // MEASURED: a gate that booted a prefix and ran a workload for 265 seconds produced NO output at all through a
    // pipeline -- not in the calling script's own failure path, not with the output echoed back. A diagnostic whose
    // result can be lost while it runs cannot be used to decide anything, and the caller cannot even tell a silent
    // failure from a lost one. Child processes inherit these descriptors, so the boot harness and the guest tooling are
    // captured too, and the announcement is written to the ORIGINAL stdout so it survives the redirection.
    // PARSE FIRST. MEASURED: with the transcript installed before parsing, `dwdiag prefix --artifact …` (a flag that had
    // been lost from the build) answered `error: unexpected argument '--artifact' found` INTO THE TRANSCRIPT, so the
    // caller saw a bare TRANSCRIPT= line and nothing else -- a usage error is an answer and must reach the caller.
    let cli = match Cli::try_parse() {
        Ok(cli) => cli,
        Err(e) => {
            print!("{e}");
            return Ok(if e.use_stderr() { ExitCode::from(2) } else { ExitCode::SUCCESS });
        }
    };
    let mut transcript_path: Option<PathBuf> = None;
    {
        use std::io::Write;
        let path = std::env::var("DWDIAG_TRANSCRIPT")
            .map(PathBuf::from)
            .unwrap_or_else(|_| {
                PathBuf::from(format!("/tmp/dwdiag-transcript-{}.out", std::process::id()))
            });
        // `--help` and `--version` are ANSWERS, not run output: redirecting them into a file would hide the one thing
        // the caller asked for. MEASURED: with the redirect unconditional, `dwdiag prefix --help` printed nothing to the
        // terminal at all.
        let wants_help = std::env::args()
            .any(|a| matches!(a.as_str(), "-h" | "--help" | "-V" | "--version" | "help"));
        if !wants_help {
            // The transcript exists for runs whose output can be LOST while they run -- a boot plus a workload under a
        // watchdog. For the short diagnostics the answer IS the output, and redirecting it hides the one thing the
        // caller asked for: MEASURED, `dwdiag symbolize` refused loudly with `Error: base symbol not found: 0` and I read
        // an empty terminal, concluding the tool was silent. Answers go to the terminal; only long runs get a transcript.
        let long_run = std::env::args().any(|a| matches!(a.as_str(), "verdict" | "suite" | "prefix" | "replay"))
            || std::env::var("DWDIAG_TRANSCRIPT").is_ok();
        if long_run {
        if let Ok(file) = std::fs::File::create(&path) {
                // Keep a duplicate of the CALLER's stdout for `say()`, then redirect.
                {
                    use std::os::fd::IntoRawFd;
                    if let Ok(d) = nix::unistd::dup(std::io::stdout()) {
                        let _ = ORIGINAL_STDOUT.set(d.into_raw_fd());
                    }
                }
                // Announce on the ORIGINAL stdout first, then redirect: the announcement must reach the caller even though
                // everything printed afterwards (including by the children) lands in the transcript.
                transcript_path = Some(path.clone());
                println!("TRANSCRIPT={}", path.display());
                let _ = std::io::stdout().flush();
                let _ = nix::unistd::dup2_stdout(&file);
                let _ = nix::unistd::dup2_stderr(&file);
            }
        }
        }
    }
    // The diagnostics do not run an experiment and do not produce a bundle: they answer a question and report it with
    // their OWN exit code (0 ok/PASS, 1 finding, 2 usage, 3 tool error), so a caller can compose them without parsing
    // human output. That is why they are dispatched before the bundle machinery.
    //
    // After a long run the ESSENTIALS are echoed to the caller from the transcript. Without this the caller sees only
    // the transcript's path: MEASURED, output was grepped from stdout and matched nothing while the answer sat in the
    // file, twice producing a wrong "the tool was silent" conclusion. The transcript still holds everything; this is
    // the part a person or a script actually asks for.
    if let RunnerCommand::Diag(cmd) = cli.command {
        let code = match diag::dispatch(cmd) {
            Ok(code) => code,
            // AN ERROR MUST REACH THE CALLER, NOT ONLY THE TRANSCRIPT. MEASURED: a long-run command has stdout and
            // stderr redirected into its transcript by design, so `dwdiag prefix --artifact <unknown-name>` failed with
            // its reason visible only inside the file and the caller read `TRANSCRIPT=...` plus `ESSENTIALS 0 line(s)`
            // as "nothing to do" -- the same silent-answer defect this file already fixed for clap's usage errors, one
            // layer further in. The reason is written to the stream the caller is watching, and the exit code is a
            // failure so a script cannot mistake it for success.
            Err(e) => {
                say(&format!("ERROR: {e:#}"));
                if let Some(p) = transcript_path.as_deref() {
                    echo_essentials(p);
                }
                return Ok(ExitCode::from(3));
            }
        };
        if let Some(p) = transcript_path.as_deref() {
            echo_essentials(p);
        }
        return Ok(code);
    }
    // The run/capture path announces its bundle before the payload starts, so the announcement is
    // available for a hang instead of only for a finished run; do not print a second, later copy.
    let announced_early = matches!(cli.command, RunnerCommand::Run(_) | RunnerCommand::Capture(_));
    let (bundle, result) = match cli.command {
        RunnerCommand::Run(args) => {
            let command = args.common.command.clone();
            run_experiment(&args.common, command, HashMap::new())?
        }
        RunnerCommand::Darling(mut args) => {
            prepare_darling(&args)?;
            let dprefix = expand_home(&args.dprefix);
            if args.common.stall_log.is_none()
                && (args.common.stall_pattern.is_some() || args.common.stall_seconds.is_some())
            {
                args.common.stall_log = Some(dprefix.join("private/var/log/dserver.log"));
            }
            if args.common.terminate_command.is_none() {
                args.common.terminate_command = Some(format!(
                    "DPREFIX={} {} shutdown",
                    shell_escape::escape(Cow::Owned(dprefix.display().to_string())),
                    shell_escape::escape(Cow::Owned(
                        expand_home(&args.darling).display().to_string()
                    ))
                ));
            }
            let (command, env) = darling_command(&args)?;
            run_experiment(&args.common, command, env)?
        }
        RunnerCommand::Capture(args) => {
            let pid = find_pid(args.pid, args.pattern.as_deref())?;
            let bundle = make_bundle(&args.bundle_root, &args.name)?;
            if args.tree {
                capture_process_tree(
                    pid,
                    &bundle,
                    args.gdb,
                    args.gdb_namespace,
                    args.gdb_executable.as_deref(),
                    args.gdb_cwd.as_deref(),
                    &args.gdb_ex,
                    args.snapshots,
                    Duration::from_secs(args.interval_seconds),
                )?;
            } else {
                capture_target(
                    pid,
                    &bundle,
                    args.gdb,
                    args.gdb_namespace,
                    args.gdb_executable.as_deref(),
                    args.gdb_cwd.as_deref(),
                    &args.gdb_ex,
                    args.snapshots,
                    Duration::from_secs(args.interval_seconds),
                )?;
            }
            (bundle, "captured")
        }
        // unreachable: the diagnostics are dispatched before this match, and they own their exit codes
        RunnerCommand::Diag(_) => unreachable!("diag dispatched before the bundle machinery"),
        RunnerCommand::Signal(args) => {
            let pid = find_pid(args.pid, args.pattern.as_deref())?;
            let signal_name = if args.signal.starts_with("SIG") {
                args.signal
            } else {
                format!("SIG{}", args.signal)
            };
            let signal: Signal = signal_name
                .parse()
                .map_err(|_| anyhow::anyhow!("invalid signal: {signal_name}"))?;
            if args.group {
                let pgid = getpgid(Some(Pid::from_raw(pid as i32)))?;
                killpg(pgid, signal)?;
            } else {
                nix::sys::signal::kill(Pid::from_raw(pid as i32), signal)?;
            }
            (PathBuf::from("."), "signaled")
        }
    };
    if !announced_early {
        println!("BUNDLE={}", bundle.display());
    }
    println!("RESULT={result}");
    if matches!(result, "exited" | "captured" | "signaled") {
        Ok(ExitCode::SUCCESS)
    } else {
        bail!("{result}")
    }
}

/// A named state predicate over the target's threads.
///
/// It exists because "the workload stopped working" is a fact about /proc, not about text: a hang can
/// print nothing, and the condition worth capturing is "these threads are parked and stay parked".
/// The grammar is deliberately tiny and each variant says what it means in `describe`, so a capture
/// never fires for an unnamed reason.
struct StateTrigger {
    kind: StateTriggerKind,
}

enum StateTriggerKind {
    State(String),
    Wchan(String),
    Syscall(String),
    AllWaiting(u64),
}

struct ThreadState {
    pid: u32,
    tid: u32,
    state: String,
    wchan: String,
    syscall: String,
}

impl StateTrigger {
    fn parse(expr: &str) -> Result<Self> {
        let (key, value) = expr
            .split_once('=')
            .with_context(|| format!("expected KEY=VALUE in --capture-on-state, got {expr:?}"))?;
        let kind = match key {
            "state" => {
                // A state letter nobody can match is a predicate that silently never fires.
                let valid = ["R", "S", "D", "T", "t", "Z", "X", "x", "I", "W"];
                if !valid.contains(&value) {
                    bail!("--capture-on-state state={value:?} is not a process state letter; use one of R S D T t Z X x I W");
                }
                StateTriggerKind::State(value.to_owned())
            }
            "wchan" => {
                if value.is_empty() {
                    bail!("--capture-on-state wchan= is empty, so it would match any wchan");
                }
                StateTriggerKind::Wchan(value.to_owned())
            }
            "syscall" => {
                if value.parse::<i64>().is_err() {
                    bail!("--capture-on-state syscall={value:?} is not a number");
                }
                StateTriggerKind::Syscall(value.to_owned())
            }
            "all-waiting" => StateTriggerKind::AllWaiting(
                value
                    .parse()
                    .with_context(|| format!("--capture-on-state all-waiting expects seconds, got {value:?}"))?,
            ),
            other => bail!("unknown --capture-on-state key {other:?}; use state, wchan, syscall or all-waiting"),
        };
        Ok(Self { kind })
    }

    fn describe(&self) -> String {
        match &self.kind {
            StateTriggerKind::State(v) => format!("state={v}"),
            StateTriggerKind::Wchan(v) => format!("wchan contains {v}"),
            StateTriggerKind::Syscall(v) => format!("syscall={v}"),
            StateTriggerKind::AllWaiting(v) => {
                format!("all {v}s: every thread of a multi-threaded target waiting")
            }
        }
    }

    fn matches(&self, pid: u32) -> Option<Vec<ThreadState>> {
        let threads = sample_threads(pid);
        if threads.is_empty() {
            return None;
        }
        match &self.kind {
            StateTriggerKind::State(v) => {
                let hits: Vec<_> = threads.into_iter().filter(|t| &t.state == v).collect();
                (!hits.is_empty()).then_some(hits)
            }
            StateTriggerKind::Wchan(v) => {
                let hits: Vec<_> = threads.into_iter().filter(|t| t.wchan.contains(v.as_str())).collect();
                (!hits.is_empty()).then_some(hits)
            }
            StateTriggerKind::Syscall(v) => {
                let hits: Vec<_> = threads.into_iter().filter(|t| &t.syscall == v).collect();
                (!hits.is_empty()).then_some(hits)
            }
            StateTriggerKind::AllWaiting(seconds) => {
                // A single-threaded process waiting for its child is normal (a compiler driver sits in
                // pipe_read while the linker runs), so a target with one thread is never "stuck"; the
                // hang this catches is a workload whose WORKERS are parked.
                if threads.len() < 2 {
                    // The condition cannot hold, so the interval that was being timed is broken and
                    // must restart: leaving it intact let a later multi-threaded wait inherit elapsed
                    // time from before, i.e. report a duration that was not continuous.
                    *all_waiting_since(pid) = None;
                    return None;
                }
                let all = threads.iter().all(|t| is_waiting(&t.state, &t.wchan));
                if !all {
                    *all_waiting_since(pid) = None;
                    return None;
                }
                let since = all_waiting_since(pid);
                let started = since.get_or_insert_with(Instant::now);
                (started.elapsed() >= Duration::from_secs(*seconds)).then_some(threads)
            }
        }
    }
}

/// When the "everyone waiting" condition started holding, keyed by pid. A module-level map is enough
/// here: one run captures one target at a time, and the value is reset the moment the condition breaks.
fn all_waiting_since(pid: u32) -> &'static mut Option<Instant> {
    static mut SINCE: Option<(u32, Option<Instant>)> = None;
    unsafe {
        match SINCE {
            Some((p, _)) if p != pid => SINCE = Some((pid, None)),
            None => SINCE = Some((pid, None)),
            _ => {}
        }
        if let Some((_, ref mut since)) = SINCE {
            since
        } else {
            unreachable!()
        }
    }
}

fn is_waiting(state: &str, wchan: &str) -> bool {
    if state.starts_with('D') || state.starts_with('S') {
        return true;
    }
    // A guest thread parked in an ulock wait can still be reported as running while it sits in the
    // server, so the wchan is the honest half of the test.
    let parked = [
        "futex", "ulock", "poll", "ep_poll", "wait", "pause", "do_sys",
    ];
    parked.iter().any(|p| wchan.contains(p))
}

fn sample_threads(pid: u32) -> Vec<ThreadState> {
    let mut out = Vec::new();
    let Ok(entries) = fs::read_dir(format!("/proc/{pid}/task")) else {
        return out;
    };
    let mut tids: Vec<u32> = entries
        .flatten()
        .filter_map(|e| e.file_name().to_string_lossy().parse::<u32>().ok())
        .collect();
    tids.sort_unstable();
    for tid in tids {
        let base = PathBuf::from(format!("/proc/{pid}/task/{tid}"));
        let state = fs::read_to_string(base.join("stat"))
            .ok()
            .and_then(|s| {
                // state is the field after the comm in parentheses
                s.rsplit_once(')').map(|(_, rest)| {
                    rest.split_whitespace().next().unwrap_or("?").to_string()
                })
            })
            .unwrap_or_else(|| "?".into());
        let wchan = fs::read_to_string(base.join("wchan"))
            .map(|s| s.trim().to_string())
            .unwrap_or_else(|_| "?".into());
        let syscall = fs::read_to_string(base.join("syscall"))
            .ok()
            .and_then(|s| s.split_whitespace().next().map(|v| v.to_string()))
            .unwrap_or_else(|| "?".into());
        out.push(ThreadState { pid, tid, state, wchan, syscall });
    }
    out
}
