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
        Ok(Some(Self {
            log,
            pattern: Regex::new(pattern).context("invalid --stall-pattern")?,
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
            self.armed = self
                .after_output
                .as_ref()
                .is_some_and(|pattern| pattern.is_match(&output));
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
            let new_events = self.pattern.find_iter(&new_text).count() as u64;
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
    let mut detector = StallDetector::from_args(args)?;
    let mut child = spawn(args, &bundle, &command, &extra_env)?;
    fs::write(bundle.join("pid.txt"), format!("{}\n", child.id()))?;
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
        if args.capture_gdb {
            let capture_pid = args
                .capture_pattern
                .as_deref()
                .and_then(|pattern| find_pid_in_tree(child.id(), pattern).ok())
                .unwrap_or(child.id());
            if args.capture_tree {
                capture_process_tree(
                    capture_pid,
                    &bundle.join("capture-tree"),
                    true,
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
    println!("BUNDLE={}", bundle.display());
    println!("RESULT={result}");
    if matches!(result, "exited" | "captured" | "signaled") {
        Ok(ExitCode::SUCCESS)
    } else {
        bail!("{result}")
    }
}
