//! Diagnostics that were previously a pile of shell scripts, folded into ONE composable CLI.
//!
//! WHY THEY LIVE HERE. The guest/runtime work needs four things constantly, and each of them was a separate script with
//! its own quoting rules, its own idea of a verdict and its own exit code (MEASURED: two workloads that never finished
//! were reported PASS by a harness whose marker matched the workload's START line; a crash was resolved by hand five
//! times; a denial's caller was derived by hand every time; and a cleanup killed its own caller because a grandparent
//! was inside a cmdline filter). They also all need the same two primitives -- `llvm-nm` symbolization and reading a
//! run log -- so they belong in one binary with:
//!
//!   * one verdict rule (`RING_MACH_TEST mode=<M> ... pass=1`, absence is FAIL/HANG, never PASS);
//!   * `--json` on every subcommand, so a caller can compose them instead of scraping human output;
//!   * stable exit codes: 0 ok/PASS, 1 verdict or acceptance failure, 2 usage, 3 tool error.
//!
//! They deliberately DELEGATE instead of reimplementing: the boot/prefix harness stays the one implementation of
//! stopping and starting a prefix (`--boot-runner`), and `llvm-nm`/`objdump` stay the one implementation of reading a
//! symbol table and a disassembly, because a second implementation of either would be a second source of truth for
//! something the toolchain already answers.

use anyhow::{Context, Result, bail};
use clap::Args;
use regex::Regex;
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};

// ----------------------------------------------------------------------------------------------------------------
// shared: symbolization
// ----------------------------------------------------------------------------------------------------------------

#[derive(Debug, Clone)]
struct Symbol {
    addr: u64,
    name: String,
}


fn load_symbols(binary: &Path) -> Result<Vec<Symbol>> {
    let out = Command::new("llvm-nm")
        .arg("-n")
        .arg(binary)
        .output()
        .with_context(|| format!("running llvm-nm on {}", binary.display()))?;
    if !out.status.success() {
        bail!(
            "llvm-nm failed for {}: {}",
            binary.display(),
            String::from_utf8_lossy(&out.stderr)
        );
    }
    let text = String::from_utf8_lossy(&out.stdout);
    let mut syms = Vec::new();
    for line in text.lines() {
        let mut parts = line.split_whitespace();
        let (Some(addr), Some(_kind), Some(name)) = (parts.next(), parts.next(), parts.next())
        else {
            continue;
        };
        if let Ok(addr) = u64::from_str_radix(addr, 16) {
            // A universal binary lists every name once per slice; the first occurrence is the x86_64 slice that the
            // deltas in this workspace are measured against, and the caller is told which base answered.
            if syms.iter().any(|s: &Symbol| s.name == name) {
                continue;
            }
            syms.push(Symbol {
                addr,
                name: name.to_string(),
            });
        }
    }
    if syms.is_empty() {
        bail!("no symbols in {}", binary.display());
    }
    Ok(syms)
}

/// `symbol + offset` for an address inside the image: the nearest symbol at or below it.
fn locate(syms: &[Symbol], target: u64) -> Option<(usize, u64)> {
    let mut best: Option<(usize, u64)> = None;
    for (i, s) in syms.iter().enumerate() {
        if s.addr <= target && best.map(|(_, a)| a < s.addr).unwrap_or(true) {
            best = Some((i, s.addr));
        }
    }
    best.map(|(i, a)| (i, target - a))
}

/// Minimal but CORRECT JSON string escaping. MEASURED: the disassembly was interpolated with only quote/backslash and
/// `\n` handled, so a raw tab or any control character made the whole document unparseable -- a tool that emits
/// "json" a consumer cannot parse is worse than one that emits text, because the consumer trusted it.
fn jesc(s: &str) -> String {
    let mut out = String::with_capacity(s.len() + 8);
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out
}

fn symbol_name(name: &str) -> &str {
    name.strip_prefix('_').unwrap_or(name)
}

// ----------------------------------------------------------------------------------------------------------------
// symbolize
// ----------------------------------------------------------------------------------------------------------------

/// Build the runtime the way the rule requires: the two compilations of the same loader code (libsystem_kernel and
/// the dyld image) are built TOGETHER, because building one leaves the other stale -- a mistake this session made
/// and measured. Replaces the long ninja invocation plus its error grep and artifact listing, which was retyped at
/// every iteration; the build tree comes from the environment so a caller does not retype a path.
#[derive(Args, Debug)]
/// Which build tree actually CONSUMES a source file, answered before a build is attempted.
///
/// MEASURED COST OF NOT ASKING: this stage edited the darlingserver's thread creator, ran `ninja darlingserver`,
/// read `ninja: no work to do`, and only then discovered that the build tree holds ZERO rules referencing that
/// source file -- the target imports a prebuilt binary, so the probe could never appear in any run. A cycle that
/// requests it would have deployed and measured the OLD runtime and reported "the instrument stayed silent",
/// which is exactly the class of false negative this tool exists to prevent.
pub struct SourceArgs {
    /// Build tree to inspect. Taken from DWDIAG_BUILD when the flag is absent.
    #[arg(long)]
    build: Option<PathBuf>,
    /// Source files whose consumption must be proven.
    #[arg(long = "source", required = true)]
    sources: Vec<PathBuf>,
    #[arg(long)]
    json: bool,
}

#[derive(Args, Debug)]
pub struct BuildArgs {
    /// Build tree. Taken from DWDIAG_BUILD when the flag is absent, so a caller does not retype a path.
    #[arg(long)]
    build: Option<PathBuf>,
    #[arg(long = "target", value_delimiter = ',', default_value = "libsystem_kernel.dylib,dyld")]
    targets: Vec<String>,
    /// Substrings that must be present in a built artifact, checked after the build. A probe whose string is absent
    /// from the artifact cannot fire, and a silent probe that is really an absent probe is this session's most
    /// expensive measurement mistake.
    #[arg(long = "expect", value_delimiter = ',')]
    expect: Vec<String>,
    #[arg(long, default_value = "src/external/xnu/darling/src/libsystem_kernel/libsystem_kernel.dylib")]
    kernel: PathBuf,
    #[arg(long, default_value = "src/external/dyld/dyld")]
    dyld: PathBuf,
    #[arg(long)]
    json: bool,
}

/// A path argument that may come from the environment: the caller then writes `dwdiag diag cycle --mode basic`
/// instead of spelling a build tree and a prefix at every iteration, while the tool still refuses to guess.
fn required_path(flag: &str, value: Option<PathBuf>, var: &str) -> Result<PathBuf> {
    match value {
        Some(p) => Ok(p),
        None => std::env::var(var)
            .map(PathBuf::from)
            .with_context(|| format!("{flag} or {var} is required")),
    }
}

fn artifact_facts(path: &Path, expect: &[String]) -> (String, u64, Vec<(String, u64)>) {
    let stamp = std::fs::metadata(path)
        .and_then(|m| m.modified())
        .map(|t| {
            let d = t.duration_since(std::time::UNIX_EPOCH).unwrap_or_default();
            let secs = d.as_secs() as i64;
            let tod = secs % 86400;
            format!("{:02}:{:02}:{:02}", tod / 3600, (tod % 3600) / 60, tod % 60)
        })
        .unwrap_or_else(|_| "absent".to_string());
    let size = std::fs::metadata(path).map(|m| m.len()).unwrap_or(0);
    let hits = expect
        .iter()
        .map(|tag| {
            let found = Command::new("strings")
                .arg(path)
                .output()
                .map(|o| String::from_utf8_lossy(&o.stdout).contains(tag.as_str()))
                .unwrap_or(false);
            (tag.clone(), if found { 1u64 } else { 0u64 })
        })
        .collect();
    (stamp, size, hits)
}

fn run_build(args: BuildArgs) -> Result<ExitCode> {
    let build = required_path("--build", args.build.clone(), "DWDIAG_BUILD")?;
    let out = Command::new("ninja")
        .args(&args.targets)
        .current_dir(&build)
        .output()
        .with_context(|| format!("running ninja in {}", build.display()))?;
    let mut text = String::from_utf8_lossy(&out.stdout).into_owned();
    text.push_str(&String::from_utf8_lossy(&out.stderr));
    let errors: Vec<&str> = text
        .lines()
        .filter(|l| l.contains("error:"))
        .take(6)
        .collect();
    let rc = out.status.code().unwrap_or(-1);
    println!(
        "BUILD targets={} rc={} errors={}",
        args.targets.join(","),
        rc,
        errors.len()
    );
    for line in errors.iter().take(3) {
        println!("  {}", line.trim());
    }
    let mut missing: Vec<String> = Vec::new();
    for path in [&args.kernel, &args.dyld] {
        let full = build.join(path);
        let (stamp, size, hits) = artifact_facts(&full, &args.expect);
        let cells: Vec<String> = hits
            .iter()
            .map(|(tag, n)| {
                if *n == 0 {
                    missing.push(tag.clone());
                }
                format!("{tag}={n}")
            })
            .collect();
        println!(
            "  artifact {} stamp={} bytes={} {}",
            full.display(),
            stamp,
            size,
            cells.join(" ")
        );
    }
    if !missing.is_empty() {
        missing.sort();
        missing.dedup();
        println!("BUILD-EXPECT-MISSING {}", missing.join(","));
    }
    if rc != 0 || !missing.is_empty() {
        return Ok(ExitCode::from(1));
    }
    Ok(ExitCode::SUCCESS)
}

/// One iteration of the loop this stage actually performs: build, deploy the pair, run the workload, then report
/// which instruments fired. Written because the same four steps were retyped as a long shell command at every
/// attempt, with the prefix, the build tree and the artifact pair spelled out each time -- and because a probe
/// census is a fact about INSTRUMENTS, not about my grep. Defaults come from DWDIAG_BUILD and DWDIAG_PREFIX.
#[derive(clap::Args, Debug)]
pub struct DeployArgs {
    /// Build tree and prefix. Taken from DWDIAG_BUILD / DWDIAG_PREFIX when the flags are absent.
    #[arg(long)]
    build: Option<PathBuf>,
    #[arg(long)]
    prefix: Option<PathBuf>,
    /// Deploy only these components (default: the whole set).
    #[arg(long = "component", value_delimiter = ',')]
    component: Vec<String>,
}

/// The component set a prefix must hold, as ONE unit.
///
/// WHY THIS SUBCOMMAND EXISTS, MEASURED: a prefix was left holding an instrumented `bin/darlingserver` from one
/// build tree while its guest libraries came from another, and the symptom was "Failed to exec launchd: No such file
/// or directory" -- a boot failure that said nothing about the real cause, cost two hours of deductions drawn from
/// logs whose workload never ran, and was found only by comparing sha256 sums of the seven components across
/// prefixes. The paths are the ones `scripts/darling-artifact-manifest.sh` owns (`built_path` / `dest_paths`), so
/// there is one mapping for both tools and not a second, drifting list.
const DEPLOY_SET: [(&str, &str, &str); 7] = [
    ("mldr", "src/startup/mldr/mldr", "libexec/darling/usr/libexec/darling/mldr"),
    ("dyld", "src/external/dyld/dyld", "usr/lib/dyld"),
    ("libsystem_kernel", "src/external/xnu/darling/src/libsystem_kernel/libsystem_kernel.dylib", "usr/lib/system/libsystem_kernel.dylib"),
    ("darlingserver", "src/external/darlingserver/darlingserver", "bin/darlingserver"),
    ("shellspawn", "src/shellspawn/shellspawn", "usr/libexec/shellspawn"),
    ("vchroot", "src/vchroot/vchroot", "usr/libexec/darling/vchroot"),
    ("launchd", "src/launchd/src/launchd", "sbin/launchd"),
];

/// Deploy the complete component set from ONE build tree, then verify every copy by sha256.
///
/// A prefix is only meaningful as a set: the components are built against each other, and a mixture of two builds
/// boots into a failure that looks like something else. So this copies all of them (or the named subset) and then
/// re-reads each destination and compares its digest with the source. Verification is not optional here: the whole
/// point is that "the prefix holds build X" is a claim the caller can trust.
fn run_deploy(a: DeployArgs) -> Result<ExitCode> {
    let build = required_path("--build", a.build.clone(), "DWDIAG_BUILD")?;
    let prefix = required_path("--prefix", a.prefix.clone(), "DWDIAG_PREFIX")?;
    let mut failures: Vec<String> = Vec::new();
    let mut deployed: usize = 0;

    for (name, built_rel, dest_rel) in DEPLOY_SET {
        if !a.component.is_empty() && !a.component.iter().any(|c| c == name) {
            continue;
        }
        let src = build.join(built_rel);
        if !src.exists() {
            failures.push(format!("{name}: build tree has no {built_rel}"));
            println!("DEPLOY {name} MISSING-IN-BUILD {built_rel}");
            continue;
        }
        let dst = prefix.join(dest_rel);
        if let Some(parent) = dst.parent() {
            fs::create_dir_all(parent).with_context(|| format!("creating {}", parent.display()))?;
        }
        let want = sha256_of(&src)?;
        install_artifact_into_prefix(src.as_path(), &dst, prefix.as_path())?;
        let got = sha256_of(&dst)?;
        let bytes = fs::metadata(&dst).map(|m| m.len()).unwrap_or(0);
        if want != got {
            failures.push(format!("{name}: deployed copy differs from source ({want} vs {got})"));
            println!("DEPLOY {name} VERIFY-FAILED dest={dest_rel} want={want} got={got}");
        } else {
            deployed += 1;
            println!("DEPLOY {name} ok sha={} bytes={} dest={dest_rel}", &want[..12.min(want.len())], bytes);
        }
    }

    if failures.is_empty() {
        println!("DEPLOY-SET ok components={deployed} prefix={} build={}", prefix.display(), build.display());
    } else {
        println!("DEPLOY-SET FAILED components={deployed} failures={}", failures.join("; "));
    }
    Ok(if failures.is_empty() { ExitCode::SUCCESS } else { ExitCode::from(3) })
}

#[derive(Args, Debug)]
pub struct CycleArgs {
    /// Build tree and prefix. Taken from DWDIAG_BUILD / DWDIAG_PREFIX when the flags are absent.
    #[arg(long)]
    build: Option<PathBuf>,
    #[arg(long)]
    prefix: Option<PathBuf>,
    #[arg(long, default_value = "basic")]
    mode: String,
    #[arg(long, default_value = "")]
    args: String,
    #[arg(long, default_value_t = 1)]
    repeat: u64,
    #[arg(long, default_value_t = 90)]
    wait: u64,
    #[arg(long)]
    skip_build: bool,
    #[arg(long = "target", value_delimiter = ',', default_value = "libsystem_kernel.dylib,dyld")]
    targets: Vec<String>,
    /// `NAME=PATH` runtime components to deploy; the default pair is libsystem_kernel and dyld, built together.
    #[arg(long = "artifact", value_parser = parse_kv, num_args = 0..)]
    artifact: Vec<(String, String)>,
    #[arg(long = "env", value_parser = parse_kv, num_args = 0..)]
    env: Vec<(String, String)>,
    /// Instrument tags to report per run; omitted reports every registered instrument that fired.
    #[arg(long = "probe", value_delimiter = ',')]
    probe: Vec<String>,
    /// DIAGNOSTIC EXECUTION POLICY, not guest environment: require that the run actually executed the
    /// workload and FAIL the command when the log carries no evidence of it. `--probe` implies it. This is
    /// deliberately separate from `--env`, which is product/guest environment and must never carry
    /// execution-policy switches.
    #[arg(long)]
    fresh: bool,
    #[arg(long)]
    json: bool,
    /// Host-side capture of /proc/<pid>/wchan for prefix processes, sampled once a second while the run
    /// proceeds. WHY THIS EXISTS: the boot failure this was written for leaves no guest evidence and its
    /// rate moves when the guest is instrumented, so the only non-perturbing question left is where the
    /// host threads of the prefix are blocked DURING the failure. It prints how many samples it took, so
    /// silence can never be mistaken for "nothing was blocked".
    #[arg(long)]
    capture_wchan: bool,
}

fn run_cycle(args: CycleArgs) -> Result<ExitCode> {
    let build = required_path("--build", args.build.clone(), "DWDIAG_BUILD")?;
    let prefix = required_path("--prefix", args.prefix.clone(), "DWDIAG_PREFIX")?;
    if !args.skip_build {
        let rc = run_build(BuildArgs {
            build: Some(build.clone()),
            targets: args.targets.clone(),
            expect: Vec::new(),
            kernel: PathBuf::from("src/external/xnu/darling/src/libsystem_kernel/libsystem_kernel.dylib"),
            dyld: PathBuf::from("src/external/dyld/dyld"),
            json: false,
        })?;
        if rc != ExitCode::SUCCESS {
            println!("CYCLE aborted at build");
            return Ok(rc);
        }
    }
    // THE LOADER IS DEPLOYED BY DEFAULT TOO. MEASURED: a cycle refreshed libsystem_kernel.dylib and dyld but left
    // the prefix's mldr alone, so a loader edit was invisible in the run and a guest-visible claim about thread
    // creation was read off a stale image -- the same false-negative class as a probe that is not in the artifact.
    // Every guest thread's creation passes through the loader, so it belongs in the default pair.
    let mut artifacts = vec![
        (
            "libsystem_kernel".to_string(),
            build
                .join("src/external/xnu/darling/src/libsystem_kernel/libsystem_kernel.dylib")
                .display()
                .to_string(),
        ),
        (
            "dyld".to_string(),
            build.join("src/external/dyld/dyld").display().to_string(),
        ),
        (
            "mldr".to_string(),
            build.join("src/startup/mldr/mldr").display().to_string(),
        ),
    ];
    for (k, v) in &args.artifact {
        artifacts.retain(|(n, _)| n != k);
        artifacts.push((k.clone(), v.clone()));
    }
    let rc = run_prefix(PrefixArgs {
        prefix: prefix.clone(),
        bootstrap_profile: None,
        install: Vec::new(),
        asset: Vec::new(),
        artifact: artifacts,
        env: args.env.clone(),
        mode: String::new(),
        args: String::new(),
        guest_command: "/private/var/tmp/ring_mach_msg_test".to_string(),
        wait: args.wait,
        json: false,
    })?;
    if rc != ExitCode::SUCCESS {
        println!("CYCLE aborted at deploy");
        return Ok(rc);
    }
    // FRESHNESS IS A WITNESS, NOT A STATEMENT, AND THIS PATH HAS NO VERDICT CACHE TO BYPASS.
    // MEASURED (2026-09-29), correcting an earlier claim of this very tool: `cycle` launches
    // `scripts/darling-boot-run.sh` through setsid and that script never invokes `west test`, so the
    // framework's identity-keyed verdict cache -- which does reuse a zero verdict -- is not in this call
    // chain at all. What looked like a cached 15-second PASS was a genuinely fast passing workload: its log
    // held 1637 port-operation lines, the workload's own RING_MACH_TEST_DURATION line and rc=0. Injecting
    // WEST_TEST_VERDICT_CACHE here therefore controlled nothing and is removed; what replaces it is a
    // witness read OUT OF THE RUN'S OWN LOG (workload progress lines, the workload's result line, and the
    // rc), and --fresh makes the absence of that witness a FAILURE instead of a plausible-looking pass.
    let require_fresh = args.fresh || !args.probe.is_empty();
    let run_env = args.env.clone();
    let va = VerdictArgs {
        prefix: Some(prefix.clone()),
        mode: args.mode.clone(),
        args: args.args.clone(),
        wait: args.wait,
        env: run_env,
        boot_runner: PathBuf::from("scripts/darling-boot-run.sh"),
        // The guest-visible path where the tool installs the fixture asset. MEASURED: /usr/bin resolves
        // through /Volumes/SystemRoot to the HOST's /usr/bin, so a fixture deployed into the prefix is
        // invisible there -- and a run of the OLD /usr/bin copy silently looked like a run of the new one.
        guest_command: "/private/var/tmp/ring_mach_msg_test".to_string(),
        guest_symbols: None,
        log: None,
        repeat: 1,
        list_modes: false,
        json: args.json,
    };
    /* HOST-SIDE WCHAN SAMPLER (see the flag). Started before the runs and stopped after the loop, so the
     * window covers the harness' own boot wait -- which is exactly the window a boot failure occupies. */
    let wchan_stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let wchan_samples: std::sync::Arc<std::sync::Mutex<Vec<String>>> =
        std::sync::Arc::new(std::sync::Mutex::new(Vec::new()));
    if args.capture_wchan {
        let stop = wchan_stop.clone();
        let store = wchan_samples.clone();
        let needle = prefix.to_string_lossy().to_string();
        std::thread::spawn(move || {
            let mut n: u64 = 0;
            while !stop.load(std::sync::atomic::Ordering::Relaxed) {
                n += 1;
                if let Ok(rd) = std::fs::read_dir("/proc") {
                    for e in rd.flatten() {
                        let name = e.file_name().to_string_lossy().to_string();
                        if !name.bytes().all(|b| b.is_ascii_digit()) {
                            continue;
                        }
                        let cmd = std::fs::read(format!("/proc/{name}/cmdline")).unwrap_or_default();
                        let cmds = String::from_utf8_lossy(&cmd).replace('\0', " ");
                        if !cmds.contains(&needle) {
                            continue;
                        }
                        let wchan = std::fs::read_to_string(format!("/proc/{name}/wchan"))
                            .unwrap_or_else(|_| "?".to_string());
                        let stat = std::fs::read_to_string(format!("/proc/{name}/stat"))
                            .unwrap_or_default();
                        let state = stat.split_whitespace().nth(2).unwrap_or("?").to_string();
                        let cut = cmds.len().min(90);
                        if let Ok(mut g) = store.lock() {
                            g.push(format!(
                                "s={n} pid={name} state={state} wchan={} cmd={}",
                                wchan.trim(),
                                &cmds[..cut]
                            ));
                            if g.len() > 8000 {
                                g.drain(0..2000);
                            }
                        }
                    }
                }
                std::thread::sleep(std::time::Duration::from_millis(1000));
            }
        });
    }
    let mut worst = ExitCode::SUCCESS;
    for i in 1..=args.repeat {
        // MEASURED flaw this resets: across a --repeat series the sampler accumulated every run's samples, so
        // `distinct-pids` on a failure mixed runs and could not say which process belonged to WHICH run.
        if args.capture_wchan {
            if let Ok(mut g) = wchan_samples.lock() {
                g.clear();
            }
        }
        let tag = format!("r{i}");
        let v = run_one_workload(&va, &tag)?;
        println!(
            "CYCLE[{i}/{}] mode={} verdict={} denied={} created={} rc={}",
            args.repeat,
            v.mode,
            v.verdict,
            v.denied,
            v.created,
            v.rc.map(|r| r.to_string()).unwrap_or_else(|| "-".to_string())
        );
        if !v.ok() {
            worst = ExitCode::from(1);
            if args.capture_wchan {
                let g = wchan_samples.lock().unwrap();
                println!("CAPTURE-WCHAN samples={} prefix={}", g.len(), prefix.display());
                /* THE TAIL ALONE WAS NOT ENOUGH (measured): the first catch of this flag printed the last 24
                 * samples, and by then every guest process of the prefix had exited, so all it showed was the
                 * harness shell waiting in do_wait -- true, and useless. What answers "where was it blocked"
                 * is each pid's LAST observed state, plus when it was last seen, so the reader can tell a
                 * process that waited for 300 seconds from one that vanished in the first second. */
                let mut last: std::collections::BTreeMap<String, String> = std::collections::BTreeMap::new();
                for line in g.iter() {
                    if let (Some(pid), _) = (line.split("pid=").nth(1).and_then(|s| s.split_whitespace().next()), ()) {
                        last.insert(pid.to_string(), line.clone());
                    }
                }
                println!("CAPTURE-WCHAN distinct-pids={}", last.len());
                for (_, line) in last.iter() {
                    println!("CAPTURE-WCHAN last {line}");
                }
                for line in g.iter().rev().take(8).collect::<Vec<_>>().iter().rev() {
                    println!("CAPTURE-WCHAN tail {line}");
                }
            }
        }
        // The witness, read from the log the run itself produced. `workload_lines` counts the workload's own
        // progress marks (its iteration/port marks and its start line); `result` is the last line that looks
        // like a result. A run with no progress lines executed nothing that can be classified, whatever its
        // verdict says, and an execution policy that demands freshness makes that a failure.
        let mut workload_lines = 0u64;
        let mut result = String::new();
        if let Ok(text) = read_log_lossy(&v.log) {
            for line in text.lines() {
                if line.contains("[rmmt]") || line.contains("ITER ") || line.contains("pc-entry")
                    || line.contains("SEM-SITE") || line.contains("make_port") || line.contains("drop_port")
                {
                    workload_lines += 1;
                }
                if line.contains("RING_MACH_TEST") || line.contains("__DWDIAG_RC=") {
                    result = line.trim().chars().take(120).collect();
                }
            }
        }
        println!(
            "FRESHNESS run={} log={} result={} workload_lines={} cache=none-in-this-path",
            tag,
            v.log.display(),
            if result.is_empty() { "<absent>" } else { result.as_str() },
            workload_lines
        );
        if require_fresh && workload_lines == 0 {
            println!("FRESHNESS-SUSPECT run={} log={} -- the log carries no workload execution evidence", tag, v.log.display());
            worst = ExitCode::from(1);
        }
        if let Ok(text) = read_log_lossy(&v.log) {
            let census = witness_census(&text);
            let wanted: Vec<(String, u64, String)> = census
                .into_iter()
                .filter(|(name, _, _)| args.probe.is_empty() || args.probe.iter().any(|p| p == name))
                .collect();
            let fired: Vec<String> = wanted
                .iter()
                .filter(|(_, n, _)| *n > 0)
                .map(|(name, n, _)| format!("{name}={n}"))
                .collect();
            let silent: Vec<String> = wanted
                .iter()
                .filter(|(_, n, _)| *n == 0)
                .map(|(name, _, _)| name.clone())
                .collect();
            println!("  instruments fired=[{}] silent=[{}] log={}", fired.join(" "), silent.join(" "), v.log.display());
        }
    }
    Ok(worst)
}

/// Watch a RUNNING guest's kernel signal dispositions, because that is where the evidence for the silent
/// SIGSEGV death lives and a shell around it could not be made reliable: the discovery loop that worked by
/// hand matched the harness's command line first, and the sampling loop written in bash took a second per
/// pass, so a two-second workload yielded two samples and no transition.
///
/// Discovery uses the handle that is actually observable from the host -- a process whose exe is the
/// prefix's loader AND whose cmdline names the guest path -- and then samples ONLY that process, at the
/// requested rate, recording SigCgt against the run log's size so a transition can be held against the
/// log's own marks. `cycle` runs underneath with --probe, so the run is fresh and not a cached verdict.
#[derive(Args, Debug)]
pub struct WatchArgs {
    #[arg(long)]
    build: Option<PathBuf>,
    #[arg(long)]
    prefix: Option<PathBuf>,
    #[arg(long, default_value = "basic")]
    mode: String,
    #[arg(long, default_value = "")]
    args: String,
    #[arg(long, default_value_t = 1)]
    repeat: u64,
    /// Guest path fragment that identifies the process to watch, matched against the host cmdline together
    /// with the loader exe. Ignored when --pid is given.
    #[arg(long, default_value = "ring_mach_msg_test")]
    pattern: String,
    /// Watch EXACTLY this host pid, taken from the workload's own raw `host_pid=` mark. This is the
    /// structural fix for a discovery loop that matched the shell, an early loader or the final workload
    /// interchangeably: the identity comes from the running workload itself, not from a /proc heuristic.
    #[arg(long)]
    pid: Option<u32>,
    #[arg(long, default_value_t = 200)]
    hz: u64,
    #[arg(long)]
    json: bool,
}

fn newest_run_log() -> Option<PathBuf> {
    let mut best: Option<(std::time::SystemTime, PathBuf)> = None;
    for entry in std::fs::read_dir("/tmp").ok()?.flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.starts_with("dwdiag-verdict-") || !name.ends_with(".log") {
            continue;
        }
        let Ok(md) = entry.metadata() else { continue };
        let Ok(mt) = md.modified() else { continue };
        if best.as_ref().map(|(t, _)| mt > *t).unwrap_or(true) {
            best = Some((mt, entry.path()));
        }
    }
    best.map(|(_, p)| p)
}

/// A cheap signature of the process's ADDRESS SPACE: how many mappings it has and the base of its first
/// libsystem_kernel mapping. An exec swaps the whole map, so a change here is the visible mark of "a new
/// image" -- which is what is needed to line the loss of a kernel signal disposition up against the exec
/// that Linux performs it on. Cheaper and more robust than comparing probe addresses across processes.
/// SigCgt as seen ACROSS the process's threads: the leader's value, the OR of every thread's, and how many
/// threads were read. Linux signal actions are process-wide, so these should agree -- and the moment they do
/// not, "the leader's SigCgt has no SIGSEGV bit" stops being a fact about the process and becomes a fact
/// about which thread the install landed on, which is exactly the question left open.
fn sigcgt_across_threads(pid: &str) -> (String, String, usize) {
    let leader = sig_line(pid, "SigCgt:").unwrap_or_default();
    let mut any: u64 = 0;
    let mut n = 0usize;
    if let Ok(entries) = std::fs::read_dir(format!("/proc/{pid}/task")) {
        for e in entries.flatten() {
            let tid = e.file_name().to_string_lossy().into_owned();
            if let Some(v) = sig_line(&tid_path(pid, &tid), "SigCgt:") {
                if let Ok(bits) = u64::from_str_radix(v.trim(), 16) {
                    any |= bits;
                    n += 1;
                }
            }
        }
    }
    (leader, format!("{any:016x}"), n)
}

fn tid_path(pid: &str, tid: &str) -> String {
    format!("/proc/{pid}/task/{tid}/status")
}

fn maps_signature(pid: &str) -> String {
    let Ok(text) = std::fs::read_to_string(format!("/proc/{pid}/maps")) else {
        return String::new();
    };
    let mut lines = 0u64;
    let mut base = String::new();
    for line in text.lines() {
        lines += 1;
        if base.is_empty() && line.contains("libsystem_kernel") {
            if let Some(range) = line.split_whitespace().next() {
                base = range.split('-').next().unwrap_or("").to_string();
            }
        }
    }
    format!("{lines}:{base}")
}

fn sig_line(pid: &str, key: &str) -> Option<String> {
    let text = std::fs::read_to_string(format!("/proc/{pid}/status")).ok()?;
    for line in text.lines() {
        if let Some(rest) = line.strip_prefix(key) {
            return Some(rest.trim().to_string());
        }
    }
    None
}

/// Answer "does this build tree compile this file?" from the build graph itself.
///
/// The check is deliberately about the GENERATED build graph, not about timestamps: a target that imports a
/// prebuilt artifact looks up to date whatever a source tree says, so the only honest question is whether any
/// rule in `build.ninja` mentions the file at all. Exit code 3 means "do not trust a run that claims to exercise
/// this source", which is how a silent no-op probe becomes a refusal instead of a measurement.
fn run_source_check(args: SourceArgs) -> Result<ExitCode> {
    let build = required_path("--build", args.build.clone(), "DWDIAG_BUILD")?;
    let mut rows: Vec<(String, usize)> = Vec::new();
    let mut missing = 0usize;
    for src in &args.sources {
        let name = src
            .file_name()
            .and_then(|s| s.to_str())
            .context("source path has no file name")?
            .to_string();
        let mut rules = 0usize;
        for entry in std::fs::read_dir(&build)
            .with_context(|| format!("reading the build tree {}", build.display()))?
        {
            let entry = match entry {
                Ok(e) => e,
                Err(_) => continue,
            };
            let path = entry.path();
            let fname = match path.file_name().and_then(|s| s.to_str()) {
                Some(f) => f,
                None => continue,
            };
            if !fname.ends_with(".ninja") {
                continue;
            }
            if let Ok(text) = read_log_lossy(&path) {
                rules += text.matches(&name).count();
            }
        }
        if rules == 0 {
            missing += 1;
        }
        rows.push((name, rules));
    }
    if args.json {
        let items: Vec<String> = rows
            .iter()
            .map(|(n, c)| format!("{{\"file\":\"{n}\",\"rules\":{c}}}"))
            .collect();
        println!(
            "{{\"build\":\"{}\",\"sources\":[{}]}}",
            build.display(),
            items.join(",")
        );
    } else {
        for (name, rules) in &rows {
            if *rules == 0 {
                println!(
                    "SOURCE-UNBUILT file={name} rules=0 build={} -- no rule in this build tree mentions it; \
a run here would exercise the OLD artifact, not this source",
                    build.display()
                );
            } else {
                println!("SOURCE-CONSUMED file={name} rules={rules} build={}", build.display());
            }
        }
    }
    if missing > 0 {
        return Ok(ExitCode::from(3));
    }
    Ok(ExitCode::SUCCESS)
}

fn run_watch(args: WatchArgs) -> Result<ExitCode> {
    let build = required_path("--build", args.build.clone(), "DWDIAG_BUILD")?;
    let prefix = required_path("--prefix", args.prefix.clone(), "DWDIAG_PREFIX")?;
    let loader_suffix = "/mldr";
    let prefix_s = prefix.display().to_string();

    // The run underneath: probes force a fresh run, which is the whole point.
    let run_prefix = prefix.clone();
    let run_build = build.clone();
    let mode = args.mode.clone();
    let run_args = args.args.clone();
    let repeat = args.repeat;
    let runner = std::thread::spawn(move || {
        run_cycle(CycleArgs {
            build: Some(run_build),
            prefix: Some(run_prefix),
            mode,
            args: run_args,
            repeat,
            wait: 90,
            skip_build: true,
            targets: vec!["libsystem_kernel.dylib".to_string(), "dyld".to_string()],
            artifact: Vec::new(),
            env: Vec::new(),
            probe: vec!["wait4".to_string()],
            fresh: true,
            json: false,
            capture_wchan: false,
        })
    });

    let pattern = args.pattern.clone();
    // The workload DECLARES its own host identity: its start line carries host_pid= from a raw Linux getpid,
    // which is the only value an outside observer can match in /proc. Prefer that over any /proc heuristic --
    // the heuristic matched the shell, an early loader and the final workload interchangeably, and the guest's
    // emulated pid is not the host's. --pid still wins when the caller has a pid in hand.
    let pinned = args.pid.map(|p| p.to_string());
    let period = std::time::Duration::from_millis((1000 / args.hz.max(1)).max(1));
    let start = std::time::Instant::now();
    let mut consumed: Vec<String> = Vec::new();
    let mut target: Option<String> = pinned.clone();
    if let Some(p) = &target {
        println!("WATCH pinned host pid={p} (from the workload's own host_pid= mark)");
    }
    let mut last: Option<String> = None;
    let mut samples = 0u64;
    let mut transitions = 0u64;
    while !runner.is_finished() || target.is_some() {
        if target.is_none() {
            // 1. the workload's own declaration, read from the newest run log
            if let Some(log) = newest_run_log() {
                if let Ok(text) = read_log_lossy(&log) {
                    if let Some(idx) = text.rfind("host_pid=") {
                        let tail = &text[idx + "host_pid=".len()..];
                        let digits: String = tail.chars().take_while(|c| c.is_ascii_digit()).collect();
                        // A declaration belongs to ONE run: the previous run's pid must never be picked up
                        // again, because pids are reused and the log keeps its old lines.
                        if !digits.is_empty()
                            && consumed.last().map(|c| c != &digits).unwrap_or(true)
                            && std::path::Path::new(&format!("/proc/{digits}")).exists()
                        {
                            println!("WATCH declared host pid={digits} (from the workload's own host_pid= mark)");
                            consumed.push(digits.clone());
                            target = Some(digits);
                            continue;
                        }
                    }
                }
            }
            // 2. the /proc heuristic, and only as a LAST RESORT: the declaration above is the identity the
            // workload itself reports, and the heuristic matched the shell twice while a workload was starting.
            if start.elapsed() < std::time::Duration::from_secs(90) {
                std::thread::sleep(std::time::Duration::from_millis(20));
                continue;
            }
            if let Ok(entries) = std::fs::read_dir("/proc") {
                for e in entries.flatten() {
                    let pid = e.file_name().to_string_lossy().into_owned();
                    if !pid.chars().all(|c| c.is_ascii_digit()) {
                        continue;
                    }
                    let exe = std::fs::read_link(format!("/proc/{pid}/exe"))
                        .map(|p| p.display().to_string())
                        .unwrap_or_default();
                    if !(exe.starts_with(&prefix_s) && exe.ends_with(loader_suffix)) {
                        continue;
                    }
                    let cmd = std::fs::read(format!("/proc/{pid}/cmdline"))
                        .map(|b| b.iter().map(|c| if *c == 0 { ' ' } else { *c as char }).collect::<String>())
                        .unwrap_or_default();
                    if cmd.contains(&pattern) && !cmd.contains("gwn-") {
                        println!("WATCH target pid={pid} exe={exe}");
                        target = Some(pid);
                        break;
                    }
                }
            }
            std::thread::sleep(std::time::Duration::from_millis(20));
            continue;
        }
        let pid = target.clone().unwrap();
        match (sig_line(&pid, "SigCgt:"), sig_line(&pid, "SigBlk:")) {
            (Some(cgt), Some(blk)) => {
                samples += 1;
                let (leader, any, nthreads) = sigcgt_across_threads(&pid);
                let sig = format!("{cgt}|{blk}|{leader}|{any}|{nthreads}");
                let maps = maps_signature(&pid);
                let key = format!("{sig}|{maps}");
                if last.as_deref() != Some(key.as_str()) {
                    let size = newest_run_log()
                        .and_then(|p| std::fs::metadata(p).ok())
                        .map(|m| m.len())
                        .unwrap_or(0);
                    println!(
                        "WATCH t={}.{:03}s pid={} SigCgt={} SigBlk={} leader={} any={} threads={} maps={} logsize={}",
                        start.elapsed().as_secs(),
                        start.elapsed().subsec_millis(),
                        pid,
                        cgt,
                        blk,
                        leader,
                        any,
                        nthreads,
                        maps,
                        size
                    );
                    transitions += 1;
                    last = Some(key);
                }
            }
            _ => {
                println!("WATCH pid={pid} gone after {samples} samples, {transitions} transition(s)");
                if pinned.is_some() {
                    break;   // an explicit --pid is a single target and a single lifetime
                }
                target = None;
                continue;    // a declared identity belongs to one run; the next run declares its own
            }
        }
        std::thread::sleep(period);
    }
    let rc = runner.join().unwrap_or(Ok(ExitCode::from(1)));
    println!("WATCH done samples={samples} transitions={transitions}");
    rc
}

#[derive(Args, Debug)]
pub struct SymbolizeArgs {
    /// Image to symbolize against (a Mach-O dylib or an ELF binary).
    #[arg(long)]
    binary: PathBuf,
    /// Symbol the reported offset is measured from.
    #[arg(long)]
    base_symbol: Option<String>,
    /// Offset from `--base-symbol`, e.g. 0x27a09.
    #[arg(long)]
    delta: Option<String>,
    /// An absolute address inside the image instead of a base+delta pair.
    #[arg(long)]
    addr: Option<String>,
    /// Also list this many symbols above and below the resolved one.
    #[arg(long, default_value_t = 0)]
    near: usize,
    #[arg(long)]
    json: bool,
}

fn parse_hex(s: &str) -> Result<u64> {
    let t = s.trim().trim_start_matches("0x");
    u64::from_str_radix(t, 16).with_context(|| format!("not a hex value: {s}"))
}

fn run_symbolize(args: SymbolizeArgs) -> Result<ExitCode> {
    let syms = load_symbols(&args.binary)?;
    let target = match (&args.addr, &args.base_symbol, &args.delta) {
        (Some(a), _, _) => parse_hex(a)?,
        (None, Some(b), Some(d)) => {
            let base = syms
                .iter()
                .find(|s| symbol_name(&s.name) == symbol_name(b))
                .with_context(|| format!("base symbol not found: {b}"))?;
            base.addr + parse_hex(d)?
        }
        _ => bail!("need --addr or (--base-symbol and --delta)"),
    };
    let (idx, off) = locate(&syms, target).context("target is below the first symbol")?;
    let loc = format!("{} + 0x{:x}", symbol_name(&syms[idx].name), off);
    if args.json {
        let near: Vec<String> = if args.near == 0 {
            Vec::new()
        } else {
            let lo = idx.saturating_sub(args.near);
            let hi = (idx + args.near + 1).min(syms.len());
            syms[lo..hi]
                .iter()
                .map(|s| format!("0x{:08x} {}", s.addr, symbol_name(&s.name)))
                .collect()
        };
        println!(
            "{{\"binary\":\"{}\",\"target\":\"0x{:x}\",\"symbol\":\"{}\",\"offset\":\"0x{:x}\",\"location\":\"{}\",\"near\":[{}]}}",
            args.binary.display(),
            target,
            symbol_name(&syms[idx].name),
            off,
            loc,
            near.iter()
                .map(|n| format!("\"{n}\""))
                .collect::<Vec<_>>()
                .join(",")
        );
    } else {
        println!("binary={}", args.binary.display());
        println!("target=0x{target:x}");
        println!("LOCATION: {loc}");
        if args.near > 0 {
            let lo = idx.saturating_sub(args.near);
            let hi = (idx + args.near + 1).min(syms.len());
            for (i, s) in syms[lo..hi].iter().enumerate() {
                let mark = if lo + i == idx { "  <== target" } else { "" };
                println!("  0x{:08x} {}{}", s.addr, symbol_name(&s.name), mark);
            }
        }
    }
    Ok(ExitCode::SUCCESS)
}

// ----------------------------------------------------------------------------------------------------------------
// crash
// ----------------------------------------------------------------------------------------------------------------

#[derive(Args, Debug)]
pub struct CrashArgs {
    /// Optional: required only for the loader-side `dserver-CRASH` line. A GUEST fatal signal is symbolized from the
    /// log alone, because the handler dumps /proc/self/maps at the moment of death and that names the image.
    #[arg(long)]
    binary: Option<PathBuf>,
    /// A run log containing a `dserver-CRASH` line.
    #[arg(long)]
    log: Option<PathBuf>,
    /// The crash line itself, when it is already in hand.
    #[arg(long)]
    line: Option<String>,
    /// Bytes of disassembly to show on each side of the faulting instruction.
    #[arg(long, default_value_t = 160)]
    context: u64,
    #[arg(long)]
    json: bool,
}

#[derive(Debug, Default)]
struct CrashLine {
    sig: String,
    addr: String,
    self_: Option<u64>,
    pc: Option<u64>,
    stack: Vec<u64>,
    raw: String,
}

fn parse_crash_line(line: &str) -> CrashLine {
    let mut c = CrashLine {
        raw: line.trim().to_string(),
        ..Default::default()
    };
    // EVERY marker is parsed out of EVERY comma-field. MEASURED: a first-match-else chain left `addr` empty, because
    // `sig=` and `addr=` appear in the SAME field of a real line (`[dserver-CRASH sig=b addr=0x0`) -- the parse looked
    // right, produced a parseable document, and silently dropped a field. Independent extraction is the fix.
    for field in line.split(',') {
        let field = field.trim();
        if let Some(i) = field.find("sig=") {
            c.sig = field[i + 4..]
                .split_whitespace()
                .next()
                .unwrap_or("")
                .to_string();
        }
        if let Some(i) = field.find("addr=") {
            c.addr = field[i + 5..]
                .split_whitespace()
                .next()
                .unwrap_or("")
                .to_string();
        }
        if let Some(i) = field.find("self=") {
            let v = field[i + 5..].split_whitespace().next().unwrap_or("");
            c.self_ = u64::from_str_radix(v.trim_start_matches("0x"), 16).ok();
        }
        if let Some(i) = field.find("pc=0x") {
            let v = field[i + 5..].split_whitespace().next().unwrap_or("");
            c.pc = u64::from_str_radix(v, 16).ok();
        }
        if let Some(i) = field.find('w') {
            let rest = &field[i + 1..];
            if let Some(eq) = rest.find('=') {
                let v = rest[eq + 1..].trim_start_matches("0x");
                if let Ok(w) = u64::from_str_radix(v.split_whitespace().next().unwrap_or(""), 16) {
                    c.stack.push(w);
                }
            }
        }
    }
    // the parsed line is the RETURN value: MEASURED, an earlier splice of this function dropped the trailing expression
    // and the compiler reported "mismatched types" -- a reminder that a source edit is a change like any other.
    c
}

// Proactive signal decoding (user directive): "sig=6" is an abbreviation every reader has to look up, and the
// answer changes the diagnosis completely -- 6 is an abort, which in C++ means an uncaught exception reached
// std::terminate, while 11 is a null/freed dereference. MEASURED need: a plane-path abort was printed as "sig=6"
// and the meaning had to be reconstructed by hand.
fn sig_meaning(sig: &str) -> Option<&'static str> {
    let digits: String = sig.chars().filter(|c| c.is_ascii_digit()).collect();
    match digits.parse::<i32>().ok()? {
        4 => Some("SIGILL: illegal instruction"),
        5 => Some("SIGTRAP: trace/breakpoint trap"),
        6 => Some(
            "SIGABRT: abort() -- in C++ almost always an uncaught exception reaching std::terminate, or an explicit abort()",
        ),
        7 => Some("SIGBUS: bus error (misaligned or unmapped access)"),
        8 => Some("SIGFPE: arithmetic exception"),
        11 => Some("SIGSEGV: invalid memory reference (null, freed, or wrong-object pointer)"),
        _ => None,
    }
}

/// Symbolize a GUEST fatal signal from a run log, with no hand-work: the handler writes
/// `[sigexc-fatal sig=... addr=... rip=... rsp=... rbp=...]` and dumps `/proc/self/maps` between
/// `[sigexc-maps-begin` and `[sigexc-maps-end]`, so the faulting instruction pointer can be attributed to an image and
/// resolved to a symbol by the tool itself. MEASURED need: doing this by hand took a parser, an offset computation and
/// a symbol lookup, and the same arithmetic had to be redone for every run.
fn symbolize_guest_fault(text: &str, json: bool) -> Result<ExitCode> {
    // The log accumulates across runs, so the FIRST fatal line can belong to an older binary that did not print
    // registers. Take the last one that carries rip= -- the newest fault is the one being asked about.
    let fatal = text
        .lines()
        .filter(|l| l.contains("[sigexc-fatal") && l.contains("rip="))
        .last()
        .context("no [sigexc-fatal line with rip= in the log (older binary)")?;
    // The marks print addresses as `0x...`, and from_str_radix does NOT accept a radix prefix -- MEASURED: the tool
    // matched the right line and then reported "carries no rip=", because the parse, not the search, was wrong.
    let grab = |key: &str| -> Option<u64> {
        fatal.split_whitespace().find_map(|w| {
            w.strip_prefix(key)
                .map(|v| v.trim_end_matches(']'))
                .map(|v| v.strip_prefix("0x").or_else(|| v.strip_prefix("0X")).unwrap_or(v))
                .and_then(|v| u64::from_str_radix(v, 16).ok())
        })
    };
    let rip = grab("rip=").context("the fatal line carries no rip= (older log)")?;
    let addr = grab("addr=").unwrap_or(0);
    let sig = fatal
        .split_whitespace()
        .find_map(|w| w.strip_prefix("sig=").and_then(|v| v.parse::<i64>().ok()))
        .unwrap_or(0);

    let mut maps: Vec<(u64, u64, u64, String)> = Vec::new();
    let mut inside = false;
    for l in text.lines() {
        if l.contains("[sigexc-maps-begin") {
            inside = true;
            continue;
        }
        if l.contains("[sigexc-maps-end") {
            break;
        }
        if !inside {
            continue;
        }
        let mut f = l.split_whitespace();
        let (Some(range), Some(_perms), Some(off)) = (f.next(), f.next(), f.next()) else { continue };
        let (Some(lo), Some(hi)) = (range.split('-').next(), range.split('-').nth(1)) else { continue };
        let (Ok(lo), Ok(hi), Ok(off)) =
            (u64::from_str_radix(lo, 16), u64::from_str_radix(hi, 16), u64::from_str_radix(off, 16))
        else {
            continue;
        };
        let rest: Vec<&str> = f.collect();
        let path = rest.last().copied().unwrap_or("").to_string();
        maps.push((lo, hi, off, path));
    }

    let (lo, _hi, file_off, path) = maps
        .iter()
        .find(|(lo, hi, _, _)| *lo <= rip && rip < *hi)
        .map(|(lo, hi, off, p)| (*lo, *hi, *off, p.clone()))
        .with_context(|| format!("rip={rip:#x} is in no dumped mapping"))?;

    println!("GUEST-FAULT sig={sig} addr={addr:#x} rip={rip:#x}");

    // THE CALLERS ABOVE THE FAULT: the handler also dumps stack words, and each one that lands in a mapped, named
    // image is a candidate return address. Reporting them makes "who called into this function" one command instead of
    // a stack walk performed by hand, which is how the earlier crashes were chased.
    let stack: Vec<u64> = text
        .lines()
        .filter_map(|l| {
            let i = l.find("[sigexc-stack ")?;
            let rest = &l[i..];
            let v = rest.split("v=").nth(1)?.trim_end_matches(']').trim();
            u64::from_str_radix(v.strip_prefix("0x").unwrap_or(v), 16).ok()
        })
        .collect();
    if !stack.is_empty() {
        println!("GUEST-STACK {} word(s)", stack.len());
        let mut shown = 0;
        for (wi, w) in stack.iter().enumerate() {
            if *w == 0 {
                continue;
            }
            // ANY mapping counts, anonymous included. MEASURED: requiring a mapping with a PATH suppressed every
            // candidate and the caller list came out empty on a log that clearly had 33 stack words -- a filter that
            // silently discards the answer it exists to find. Anonymous regions are printed as such, so the first
            // pass through a real chain has something to follow.
            let Some((lo, hi, foff, p)) =
                maps.iter().find(|(lo, hi, _, _)| *lo <= *w && *w < *hi).map(|(a, b, c, d)| (*a, *b, *c, d.clone()))
            else {
                continue;
            };
            if p.is_empty() {
                println!("GUEST-CALLER w{wi}={w:#x} <anonymous mapping {lo:#x}-{hi:#x}>");
                continue;
            }
            let target = foff + (*w - lo);
            // NO FILTER AND NO INVERTED PREDICATE. MEASURED: an edit-time rewrite of this very expression dropped its
            // leading `!`, so retain() KEPT the numeric local labels and threw the real names away -- the caller list
            // then reported "0 symbols loaded" for images that llvm-nm resolves. Names are printed as the table has
            // them; a reader can see a numeric label instead of the row being silently deleted.
            let Ok(syms) = load_symbols(Path::new(&p)) else { continue };
            let file = Path::new(&p).file_name().map(|f| f.to_string_lossy().to_string()).unwrap_or_default();
            match locate(&syms, target) {
                Some((idx, delta)) => println!(
                    "GUEST-CALLER w{wi}={w:#x} {}+{:#x} ({file} file offset {target:#x})",
                    symbol_name(&syms[idx].name),
                    delta
                ),
                // No symbol is still an answer: it says the word lands in a mapped image at an offset with no name in
                // the table, which is what a stripped region or a data word looks like.
                None => println!(
                    "GUEST-CALLER w{wi}={w:#x} <no symbol, {} symbols loaded> ({file} file offset {target:#x})",
                    syms.len()
                ),
            }
            shown += 1;
            if shown >= 8 {
                break;
            }
        }
    }
    println!("GUEST-IMAGE {path} base={lo:#x}");
    if path.is_empty() {
        bail!("the faulting mapping is anonymous: nothing to symbolize");
    }
    let target = file_off + (rip - lo);
    let mut syms = load_symbols(Path::new(&path))?;
    // NUMERIC NAMES ARE LOCAL LABELS, NOT FUNCTIONS. MEASURED: the lookup returned "1106 +0x80bf0" for an address a
    // hand computation resolved to "_fgetln+0x11a" -- the nearest symbol happened to be an unnamed local label, which
    // is worse than useless because it looks like an answer. Drop them before locating.
    syms.retain(|s| {
        let n = symbol_name(&s.name);
        let numeric_label = !n.is_empty() && n.chars().all(|c| c.is_ascii_digit() || c == '.');
        !numeric_label
    });
    match locate(&syms, target) {
        // locate returns (INDEX, base) -- not a name. MEASURED: destructuring it as (name, base) printed an index as if
        // it were a symbol, which is how the output read "1106 +0x80bf0" for an address llvm-nm resolves to
        // "_fgetln+0x11a".
        // locate returns (index, DELTA) -- the second element is already the offset from that symbol, so subtracting
        // again produced "fgetln +0x80bf0" for an address that is fgetln+0x11a.
        Some((idx, delta)) => {
            println!("GUEST-SYMBOL {} +{:#x} (file offset {:#x})", symbol_name(&syms[idx].name), delta, target);
        }
        None => println!("GUEST-SYMBOL <none> (file offset {:#x})", target),
    }
    if json {
        println!(
            "{{\"sig\":{sig},\"addr\":\"{addr:#x}\",\"rip\":\"{rip:#x}\",\"image\":{path:?},\"file_offset\":\"{target:#x}\"}}"
        );
    }
    Ok(ExitCode::SUCCESS)
}

/// The newest verdict log that actually CONTAINS something this command can symbolize. MEASURED: taking simply the
/// newest log picked one written by a binary that predates the rip/rsp instrumentation, so the obvious invocation
/// failed on a log that had nothing to do with the question. Scanning a few candidates and naming the one chosen turns
/// "it did not work" into "here is the log I read".
fn newest_usable_crash_log() -> Option<String> {
    let mut candidates: Vec<(std::time::SystemTime, PathBuf)> = Vec::new();
    for entry in fs::read_dir("/tmp").ok()? {
        let Ok(e) = entry else { continue };
        let name = e.file_name().to_string_lossy().to_string();
        if !(name.starts_with("dwdiag-verdict-") && name.ends_with(".log")) {
            continue;
        }
        let Ok(md) = e.metadata() else { continue };
        let Ok(mtime) = md.modified() else { continue };
        candidates.push((mtime, e.path()));
    }
    candidates.sort_by(|a, b| b.0.cmp(&a.0));
    for (_, path) in candidates.iter().take(8) {
        let Ok(text) = read_log_lossy(path) else { continue };
        // BOTH facts must be on the SAME line: `rip=` appears in other marks too, so a file-wide contains() selected a
        // log whose fatal line predates the instrumentation -- exactly the failure this selector exists to avoid.
        let usable = text.lines().any(|l| l.contains("[sigexc-fatal") && l.contains("rip="))
            && text.contains("[sigexc-maps-begin");
        if usable {
            println!("CRASH-LOG {} (newest log with a symbolized fault)", path.display());
            return Some(text);
        }
    }
    None
}

fn run_crash(args: CrashArgs) -> Result<ExitCode> {
    // A guest fatal signal carries everything needed in the log itself; try that first, and when neither --log nor
    // --line is given, use the newest verdict log so the obvious invocation just works.
    let log_text = match (&args.log, &args.line) {
        (Some(log), _) => read_log_lossy(log).ok(),
        (None, None) => newest_usable_crash_log(),
        _ => None,
    };
    if let Some(text) = &log_text {
        if text.contains("[sigexc-fatal") && text.contains("[sigexc-maps-begin") {
            return symbolize_guest_fault(text, args.json);
        }
    }
    let line = match (&args.line, &args.log) {
        (Some(l), _) => l.clone(),
        (None, Some(log)) => {
            let text =
                read_log_lossy(log).with_context(|| format!("reading {}", log.display()))?;
            text.lines()
                .find(|l| l.contains("dserver-CRASH"))
                .map(|l| l.to_string())
                .with_context(|| format!("no dserver-CRASH line in {}", log.display()))?
        }
        _ => bail!("need --line or --log"),
    };
    let crash = parse_crash_line(&line);
    let binary = args
        .binary
        .as_ref()
        .context("a loader-side dserver-CRASH line needs --binary <image> (a guest fatal signal does not)")?;
    let syms = load_symbols(binary)?;

    // The probe reports `self` (a known symbol's runtime address) precisely so that the runtime pc can be turned into an
    // offset inside the FILE; without that subtraction the number is only meaningful to the process that printed it.
    let file_target = match (crash.self_, crash.pc) {
        (Some(s), Some(pc)) => {
            let self_file = syms
                .iter()
                .find(|s| symbol_name(&s.name).contains("dserver_crash_probe"))
                .map(|s| s.addr)
                .context(
                    "dserver_crash_probe not in the symbol table: cannot derive the file offset",
                )?;
            Some(self_file + (pc.saturating_sub(s)))
        }
        _ => crash.pc,
    };

    // HONESTY BOUND (perf#30, doc section 253). The anchor subtraction assumes the faulting pc belongs to THIS binary.
    // MEASURED: a `std::length_error` throw made the pc land in libstdc++, the subtraction produced a huge number, and
    // `locate` still returned a symbol -- the output claimed `LOCATION: end + 0x1b6f...`, a location that cannot exist.
    // An answer that cannot be checked is worse than no answer, so a target outside the binary's own symbol range is
    // reported as such instead of being matched to the nearest symbol.
    let (min_sym, max_sym) = match (syms.first(), syms.last()) {
        (Some(f), Some(l)) => (f.addr, l.addr),
        _ => (0, u64::MAX),
    };
    let in_binary = file_target
        .map(|t| t >= min_sym.saturating_sub(0x1000) && t <= max_sym.saturating_add(0x1000));
    let location = match (file_target, in_binary) {
        (Some(t), Some(true)) => locate(&syms, t).map(|(i, o)| (t, i, o)),
        _ => None,
    };
    let mut stack_locs: Vec<(usize, u64, String)> = Vec::new();
    if let Some(self_runtime) = crash.self_ {
        for (i, w) in crash.stack.iter().enumerate() {
            if *w == 0 {
                continue;
            }
            let delta = w.saturating_sub(self_runtime);
            if delta < 0x80_0000 {
                if let Some((si, off)) = locate(
                    &syms,
                    syms.iter()
                        .find(|s| symbol_name(&s.name).contains("dserver_crash_probe"))
                        .map(|s| s.addr)
                        .unwrap_or(0)
                        + delta,
                ) {
                    stack_locs.push((
                        i,
                        *w,
                        format!("{} + 0x{:x}", symbol_name(&syms[si].name), off),
                    ));
                }
            }
        }
    }

    let mut disassembly = String::new();
    if let (Some(t), Some(ctx)) = (file_target, Some(args.context)) {
        let lo = t.saturating_sub(ctx);
        let hi = t + ctx;
        let out = Command::new("objdump")
            .args([
                "-d",
                &format!("--start-address=0x{lo:x}"),
                &format!("--stop-address=0x{hi:x}"),
            ])
            .arg(binary)
            .output()
            .context("running objdump")?;
        for l in String::from_utf8_lossy(&out.stdout).lines() {
            let mut marked = l.to_string();
            if let Some(colon) = l.find(':') {
                if let Ok(a) = u64::from_str_radix(l[..colon].trim(), 16) {
                    if a == t {
                        marked.push_str("   <=== FAULT HERE");
                    }
                }
            }
            disassembly.push_str(&marked);
            disassembly.push('\n');
        }
    }

    // Proactive enrichment (user directive): a fault whose pc is OUTSIDE this binary is still evidence, and the run
    // log usually carries the panic backtrace that says where it happened. MEASURED need: the plane-path abort
    // printed `LOCATION: <outside this binary's symbol range>` and an empty disassembly, while the log's own eleven
    // `darlingserver(+0x...)` frames named the call chain -- the tool had the information and withheld it.
    let signal_meaning = sig_meaning(&crash.sig);
    let mut backtrace_frames: Vec<(u64, String)> = Vec::new();
    let backtrace_log = args
        .log
        .clone()
        .unwrap_or_else(|| newest_verdict_log().unwrap_or_default());
    if let Ok(text) = read_log_lossy(&backtrace_log) {
        for line in text.lines() {
            let Some(open) = line.find("(+0x") else {
                continue;
            };
            let name = &line[..open];
            if !(name == "darlingserver" || name.ends_with("/darlingserver")) {
                continue;
            }
            let rest = &line[open + 4..];
            let Some(end) = rest.find(')') else { continue };
            if let Ok(off) = u64::from_str_radix(&rest[..end], 16) {
                if let Some((si, o)) = locate(&syms, off) {
                    backtrace_frames
                        .push((off, format!("{} + 0x{:x}", symbol_name(&syms[si].name), o)));
                }
            }
        }
    }
    // The `addr` field of a Linux signal carries two 32-bit halves for a tgkill-raised signal; printing the halves
    // is a hypothesis, not a claim, so it is labelled as one.
    let addr_halves = {
        let a = crash.addr.trim().trim_start_matches("0x");
        u64::from_str_radix(a, 16).ok().and_then(|v| {
            if v >> 32 == 0 {
                None
            } else {
                Some((v >> 32, v & 0xffff_ffff))
            }
        })
    };

    if args.json {
        let escaped = jesc(&disassembly);
        println!(
            "{{\"crash\":\"{}\",\"sig\":\"{}\",\"addr\":\"{}\",\"location\":\"{}\",\"stack\":[{}],\"signal_meaning\":\"{}\",\"frames\":[{}],\"disassembly\":\"{}\"}}",
            jesc(&crash.raw),
            jesc(&crash.sig),
            jesc(&crash.addr),
            location
                .as_ref()
                .map(|(_, i, o)| format!("{} + 0x{:x}", symbol_name(&syms[*i].name), o))
                .unwrap_or_default(),
            stack_locs
                .iter()
                .map(|(i, _, s)| format!("{{\"w\":{},\"location\":\"{}\"}}", i, s))
                .collect::<Vec<_>>()
                .join(","),
            jesc(signal_meaning.unwrap_or("")),
            backtrace_frames
                .iter()
                .map(|(off, s)| format!(
                    "{{\"offset\":\"0x{off:x}\",\"location\":\"{}\"}}",
                    jesc(s)
                ))
                .collect::<Vec<_>>()
                .join(","),
            escaped
        );
    } else {
        println!("crash: {}", crash.raw);
        if let Some((t, i, o)) = &location {
            println!("target=0x{t:x}");
            println!("LOCATION: {} + 0x{o:x}", symbol_name(&syms[*i].name));
        } else if crash.pc.is_some() {
            // Say WHY there is no location, instead of printing nothing (or a guess): the pc is not in this binary,
            // which is the normal case for a C++ throw, a libc abort, or a fault inside a shared library.
            println!(
                "LOCATION: <outside this binary's symbol range: the pc belongs to another object>"
            );
        }
        match signal_meaning {
            Some(m) => println!("signal: {} -- {m}", crash.sig),
            None => println!("signal: {}", crash.sig),
        }
        if let Some((hi, lo)) = addr_halves {
            println!(
                "addr-halves: high=0x{hi:x} (=pid {hi}?) low=0x{lo:x} (={lo}) -- HYPOTHESIS, unverified"
            );
        }
        if !backtrace_frames.is_empty() {
            println!(
                "--- backtrace frames from the log, resolved against {} ---",
                binary.display()
            );
            for (n, (off, sym)) in backtrace_frames.iter().enumerate() {
                println!("  #{n} +0x{off:x} -> {sym}");
            }
        }
        if !stack_locs.is_empty() {
            println!("--- stack walk ---");
            for (i, _, s) in &stack_locs {
                println!("  w{i} -> {s}");
            }
        }
        print!("--- disassembly ---\n{disassembly}");
    }
    // A crash is a finding, not a tool error: exit 1 so a caller can branch on it without parsing.
    Ok(ExitCode::from(1))
}

// ----------------------------------------------------------------------------------------------------------------
// verdict / suite
// ----------------------------------------------------------------------------------------------------------------

#[derive(Args, Debug)]
pub struct VerdictArgs {
    /// The prefix to run in. Not required with --log, which judges a log that already exists: a verdict rule must be
    /// testable against a file, and paying a full prefix boot just to re-read an old verdict is what this avoids.
    #[arg(long)]
    prefix: Option<PathBuf>,
    #[arg(long, default_value = "")]
    mode: String,
    #[arg(long, default_value = "")]
    args: String,
    #[arg(long, default_value_t = 70)]
    wait: u64,
    #[arg(long, value_parser = parse_kv)]
    env: Vec<(String, String)>,
    /// The harness that owns prefix start/stop and the run log. Reused, never reimplemented.
    #[arg(long, default_value = "scripts/darling-boot-run.sh")]
    boot_runner: PathBuf,
    /// The guest workload binary the mode is an argument of.
    #[arg(long, default_value = "/usr/bin/ring_mach_msg_test")]
    guest_command: String,
    /// Image used to symbolize a denial's caller offset.
    #[arg(long)]
    guest_symbols: Option<PathBuf>,
    /// Judge a run log that ALREADY EXISTS instead of running a workload. The prefix is then unused, which is what
    /// makes a verdict rule testable against a file and what stops paying a full prefix boot to re-read an old verdict.
    #[arg(long)]
    log: Option<PathBuf>,
    /// How many times to run the SAME workload. A single-run verdict cannot see flake, and this session's
    /// evidence is full of one-run PASS/CRASH flips; the summary names the distribution and fails if it is not
    /// uniform. Each run gets its own log.
    #[arg(long, default_value_t = 1)]
    repeat: u64,
    /// Print the workload modes THIS prefix's fixture actually contains, with the argument shape and the marker each mode
    /// emits, then exit. The list is read out of the fixture's own strings, so it cannot drift from the binary that will
    /// run: the tool answers "what can I ask for, and what will it print" without anyone grepping a source tree.
    #[arg(long)]
    list_modes: bool,
    #[arg(long)]
    json: bool,
}

fn parse_kv(s: &str) -> Result<(String, String), String> {
    match s.split_once('=') {
        Some((k, v)) => Ok((k.to_string(), v.to_string())),
        None => Err("expected KEY=VALUE".to_string()),
    }
}

#[derive(Debug)]
pub struct Verdict {
    pub mode: String,
    pub verdict: String,
    pub denied: u64,
    pub created: u64,
    pub line: String,
    pub denial_call: Option<String>,
    pub denial_location: Option<String>,
    pub log: PathBuf,
    /// The guest workload's own exit status, as printed by the guest shell (`__DWDIAG_RC=`).
    pub rc: Option<i32>,
    /// Signal name when rc == 128 + N, so a crash is never reported as a hang.
    pub signal: Option<String>,
}

impl Verdict {
    fn ok(&self) -> bool {
        self.verdict == "PASS"
    }
}

/// 128 + N naming for the signals a guest workload actually dies of, so the verdict says the cause.
pub fn signal_name(n: i32) -> &'static str {
    match n {
        4 => "ILL",
        6 => "ABRT",
        7 => "BUS",
        8 => "FPE",
        11 => "SEGV",
        13 => "PIPE",
        15 => "TERM",
        9 => "KILL",
        _ => "SIG",
    }
}

/// Judge a run log that already exists: the result line, the verdict, and the workload's own exit status.
///
/// Split out of the run path for two reasons. First, a log must be re-judgeable WITHOUT a prefix -- re-running a
/// prefix-backed workload only to re-read its verdict costs about two minutes per attempt and was done repeatedly
/// during the acceptance work. Second, the rule below is exactly the kind that has to be testable against a file:
///
/// THE HARNESS'S OWN KILL IS NOT THE WORKLOAD'S CRASH. MEASURED: when the watchdog bound is reached the harness
/// terminates the run, the guest shell then reports 139 for the child it killed, and the verdict said CRASH SEGV --
/// sending the operator after a memory fault that never happened, while the log's last guest line and the server's
/// own stall dump both described a stranded waiter. The harness prints whether it reached the bound ("waited Ns of at
/// most Ns"), and that line decides the verdict before any signal does.
/// Read a run log as text, NEVER as strictly-valid UTF-8.
///
/// MEASURED DEFECT THIS EXISTS TO FIX: a run log carries whatever the guest and the loader wrote to fd 2, including
/// raw register/pointer dumps of probes that deliberately bypass libc. `fs::read_to_string` fails on the first
/// invalid byte, and every caller here treated that failure as "no log text": the verdict then reported NO-RUN for a
/// run that had PRINTED ITS OWN RESULT LINE, and the only visible evidence was an error buried in a transcript. A
/// diagnosis pipeline that can silently discard a passing run's evidence is worse than no pipeline, so logs are read
/// lossily and the bytes that are not text are replaced rather than allowed to erase the run.
/// Resolve the runtime prefix a DIAGNOSIS should read, in order: an explicit flag, DWDIAG_PREFIX, then the identity
/// line the run itself wrote.
///
/// MEASURED DEFECT THIS EXISTS TO FIX: several diagnostics carried a hardcoded default of /tmp/dr-on-matched, so a
/// run served by another prefix had its server-side evidence read from the WRONG prefix's log -- and an absence there
/// reads like "the server never sent it". The runs now stamp `[dwdiag-env prefix=...]` into their own log, so the log
/// can name the prefix that produced it and no diagnosis has to assume one.
fn resolve_prefix(explicit: Option<PathBuf>, log: Option<&std::path::Path>) -> Option<PathBuf> {
    if let Some(p) = explicit {
        return Some(p);
    }
    if let Ok(v) = std::env::var("DWDIAG_PREFIX") {
        if !v.is_empty() {
            return Some(PathBuf::from(v));
        }
    }
    let log = log?;
    let text = read_log_lossy(log).ok()?;
    for line in text.lines() {
        if let Some(rest) = line.split("prefix=").nth(1) {
            let p = rest.split_whitespace().next()?.trim_end_matches(']');
            if !p.is_empty() {
                return Some(PathBuf::from(p));
            }
        }
    }
    None
}

fn read_log_lossy(path: &std::path::Path) -> std::io::Result<String> {
    Ok(String::from_utf8_lossy(&std::fs::read(path)?).into_owned())
}

fn judge_run_log(text: &str, mode: &str) -> (String, String, Option<i32>) {
    // THE RUN NEVER STARTED, AND THAT IS ITS OWN VERDICT. MEASURED, PAINFULLY: a scratch prefix lost bin/shellspawn,
    // every subsequent run therefore failed to launch (the guest log carries "Failed to exec launchd: No such file or
    // directory" and "Rootless shellspawn did not become ready within 30000ms"), and this function reported HANG
    // (watchdog) or NO-RUN for ten consecutive runs -- verdicts that read as statements about the workload while the
    // workload had never executed a single instruction. Two hours of deductions were drawn from those logs before
    // the tail of one of them was read by hand. A launch failure must be named as a launch failure, so it is checked
    // BEFORE every workload-shaped rule below and short-circuits them.
    for (marker, label) in [
        ("Failed to exec launchd", "launchd"),
        ("shellspawn did not become ready", "shellspawn"),
        ("runtime prefix has no recognized stable state", "prefix-state"),
        ("no recognized stable state", "prefix-state"),
    ] {
        if text.contains(marker) {
            eprintln!("BOOT-FAIL ({label}): the run never reached the workload; verdict is about the prefix, not the test");
            return (String::new(), format!("BOOT-FAIL ({label})"), None);
        }
    }

    let line = workload_result_line(text, mode)
        .map(|l| l.trim().to_string())
        .unwrap_or_default();
    let started = text.contains(&format!("mode={mode}"));
    let rc: Option<i32> = text
        .lines()
        .filter_map(|l| l.split("__DWDIAG_RC=").nth(1))
        .filter_map(|v| v.trim().parse::<i32>().ok())
        .next_back();
    // A log whose guest reports SIGTERM and that carries no result line is a run the harness stopped: the guest's
    // own fatal line names the signal that ended it, and the workload never signals itself. This covers logs written
    // before the "waited ... of at most ..." line was added to them, and it is the same claim, spelled differently.
    // THE SHELL'S OWN REPORT OF HOW THE WORKLOAD DIED OUTRANKS THE BOUNDARY. MEASURED: a run whose guest
    // shell prints "/bin/bash: line 1: <pid> Segmentation fault: 11 (core dumped) <workload> <args>" was called
    // HANG (watchdog) by the previous form of this rule, because the harness did reach its bound afterwards --
    // but the workload had really died of SIGSEGV and the run never produced a result line. The shell's report is
    // the workload's own status, so it decides first. A `dserver-CRASH` probe line is NOT sufficient on its own:
    // it appears in passing runs too (teardown aborts the host-side image), so a PASS with a result line stays PASS.
    // THE GUEST'S OWN CRASH RECORD OUTRANKS EVERYTHING ELSE. MEASURED: with the workload's deliberate-fault hatch
    // the reporter prints "RING_MACH_TEST_CRASH sig=11 addr=0x0 pc=... " plus symbolized frames, and the verdict
    // still said HANG (watchdog) because no shell line named a signal -- the guest handler had caught the fault and
    // exited on its own. A guest-reported fault IS a crash and must be named as one.
    let guest_crash = text.lines().find_map(|l| {
        let i = l.find("RING_MACH_TEST_CRASH sig=")?;
        let rest = &l[i + "RING_MACH_TEST_CRASH sig=".len()..];
        rest.split_whitespace().next()?.parse::<i32>().ok()
    });
    let shell_signal = text.lines().find_map(|l| {
        if !(l.contains("/bin/bash: line") || l.contains("bash: line")) {
            return None;
        }
        for (word, sig) in [
            ("Segmentation fault", 11),
            ("Aborted", 6),
            ("Killed", 9),
            ("Floating point exception", 8),
            ("Bus error", 7),
        ] {
            if l.contains(word) {
                return Some((sig, word));
            }
        }
        None
    });
    let guest_terminated = text.lines().any(|l| l.contains("[sigexc-default sig=15"));
    let hit_bound = guest_terminated
        || text
        .lines()
        .filter_map(|l| {
            let r = l.trim().strip_prefix("waited ")?;
            let (a, b) = r.split_once("s of at most ")?;
            let a = a.trim().parse::<u64>().ok()?;
            let b = b.trim().trim_end_matches('s').parse::<u64>().ok()?;
            Some(a == b)
        })
        .any(|x| x);
    let verdict = if line.is_empty() {
        if let Some(sig) = guest_crash {
            format!("CRASH {} (guest reporter)", signal_name(sig))
        } else if let Some((sig, word)) = shell_signal {
            format!("CRASH {} ({word})", signal_name(sig))
        } else if hit_bound {
            "HANG (watchdog)".to_string()
        } else {
            match (started, rc) {
                (_, Some(c)) if c >= 128 => format!("CRASH {}", signal_name(c - 128)),
                (_, Some(c)) => format!("EXIT rc={c}"),
                (true, None) => "HANG".to_string(),
                (false, None) => "NO-RUN".to_string(),
            }
        }
    } else if line.contains("pass=1") {
        "PASS".to_string()
    } else {
        "FAIL".to_string()
    };
    (line, verdict, rc)
}

/// The identity of the runtime a run is about to use, written beside and into its log.
///
/// MEASURED COST OF NOT HAVING THIS: a run log records what the guest and the loader printed, but not WHICH copy of
/// the loader served it. Two prefixes existed -- one freshly deployed, one stale -- and a log served by the stale
/// copy was read as evidence about the fresh build, twice, before the mistake was caught by hand. The fingerprint
/// below makes every log self-attributing: the resolved prefix and the sha256 of each runtime artifact found there,
/// printed before the workload starts, written to a sidecar file (the run truncates its own log), and appended to
/// the log once the run has finished.
/// Report the prefix prerequisites a run needs, BEFORE it is attempted.
///
/// MEASURED, AND IT COST HOURS: a scratch prefix lost `bin/shellspawn`; every run after that failed to launch, and the
/// only witness was the harness's own line buried in the guest log ("Failed to exec launchd: No such file or
/// directory", "shellspawn did not become ready"). Ten verdicts read as HANG or NO-RUN -- statements about a workload
/// that never executed -- before anyone read that line. A missing prerequisite is a fact about the PREFIX, it is
/// cheap to check, and it belongs in front of the run rather than after the diagnosis.
fn prefix_prereq_report(prefix: &std::path::Path) -> bool {
    // THE REAL DESTINATION PATHS, taken from scripts/darling-artifact-manifest.sh (dest_paths) rather than guessed.
    // MEASURED FALSE ALARM: the first version of this check looked for "bin/shellspawn", a path that exists in NO
    // prefix -- not even in a healthy one -- because shellspawn is deployed to usr/libexec/shellspawn. The check
    // reported "PREFIX-PREREQ MISSING" for a prefix that was in fact fine, and a guard that cries wolf is worse than
    // no guard: it sends the next reader to repair something that is not broken. One representative destination per
    // component is enough; the manifest owns the full list.
    const NEEDED: [&str; 5] = [
        "usr/libexec/shellspawn",
        "bin/darlingserver",
        "sbin/launchd",
        "libexec/darling/usr/libexec/darling/mldr",
        "usr/lib/dyld",
    ];
    let mut missing: Vec<&str> = Vec::new();
    for rel in NEEDED {
        if !prefix.join(rel).exists() {
            missing.push(rel);
        }
    }
    if missing.is_empty() {
        println!("PREFIX-PREREQ ok prefix={} checked={}", prefix.display(), NEEDED.len());
        true
    } else {
        println!(
            "PREFIX-PREREQ MISSING prefix={} missing={} -- a run on this prefix fails to launch; fix the prefix \
(repair it or bootstrap a fresh one) before reading any verdict as a statement about the workload",
            prefix.display(),
            missing.join(",")
        );
        false
    }
}


/// The processes that hold a prefix's files open, found the only way that works from the host.
///
/// MEASURED, AND IT STOPPED A GATE RUN: an aborted cycle left darlingserver (reparented to pid 1), launchd and
/// shellspawn alive; the next install of `mldr` failed with ETXTBSY and the harness reported only "installing
/// .../mldr (after a shutdown attempt; first error: Text file busy)". `bin/darling --rootless shutdown` stops the
/// server the harness knows about, not the guest processes it does not, so the retry failed the same way and the
/// message named neither the file's holders nor what to do. A file lock is a fact about processes, so report the
/// processes.
///
/// Matching is on `exe` AS WELL AS `cmdline`, because a guest process runs through the prefix's `mldr` (its `exe`
/// is the loader, its `cmdline` is the guest argv) while the server's `cmdline` names the prefix without `exe` doing
/// so. The caller's own ancestry is excluded: a blanket match must never reach the process asking the question, and
/// killing an ancestor at any depth ends the caller instead of the holder (that mistake was made once already).
fn prefix_processes(prefix: &std::path::Path) -> Vec<u32> {
    let needle = prefix.to_string_lossy().to_string();
    let mut excluded: Vec<u32> = Vec::new();
    let mut pid = std::process::id();
    while pid > 1 {
        excluded.push(pid);
        let stat = match fs::read_to_string(format!("/proc/{pid}/stat")) {
            Ok(s) => s,
            Err(_) => break,
        };
        pid = match stat.rsplit(')').next().and_then(|tail| tail.split_whitespace().nth(1)) {
            Some(v) => v.parse().unwrap_or(1),
            None => break,
        };
    }
    let mut found: Vec<u32> = Vec::new();
    if let Ok(entries) = fs::read_dir("/proc") {
        for entry in entries.flatten() {
            let name = entry.file_name().to_string_lossy().to_string();
            let other: u32 = match name.parse() {
                Ok(v) => v,
                Err(_) => continue,
            };
            if excluded.contains(&other) {
                continue;
            }
            let exe = fs::read_link(format!("/proc/{other}/exe")).ok();
            let cmdline = fs::read(format!("/proc/{other}/cmdline")).unwrap_or_default();
            let exe_hit = exe.as_ref().map(|p| p.to_string_lossy().contains(&needle)).unwrap_or(false);
            let cmd_hit = String::from_utf8_lossy(&cmdline).contains(&needle);
            if exe_hit || cmd_hit {
                found.push(other);
            }
        }
    }
    found.sort_unstable();
    found
}

/// Stop every process holding the prefix, settle, then escalate. Returns the pids we found (for the message).
fn stop_prefix_holders(prefix: &std::path::Path) -> Vec<u32> {
    let victims = prefix_processes(prefix);
    for &pid in &victims {
        unsafe { libc_kill(pid as i32, 15) };
    }
    std::thread::sleep(std::time::Duration::from_secs(3));
    for &pid in &victims {
        if Path::new(&format!("/proc/{pid}")).exists() {
            unsafe { libc_kill(pid as i32, 9) };
        }
    }
    std::thread::sleep(std::time::Duration::from_millis(500));
    victims
}

unsafe extern "C" {
    #[link_name = "kill"]
    fn libc_kill(pid: i32, sig: i32) -> i32;
}


/// Install ONE file into a prefix, removing whatever holds it.
///
/// There is exactly ONE way to install a file into a prefix, and this is it: both `deploy` and the cycle's staging
/// path call it, because a second implementation of the same step drifts and then one of them is the one that fails
/// at 3 a.m. The order is: copy, and when that fails, `bin/darling --rootless shutdown`, then stop the processes that
/// still hold the file (a surviving launchd/shellspawn keeps `mldr` busy -- ETXTBSY is a fact about processes, not
/// about permissions), then retry once and report the holders if it still fails.
fn install_artifact_into_prefix(
    built: &Path,
    dest_path: &Path,
    prefix: &std::path::Path,
) -> Result<()> {
    let source = built.to_path_buf();
    if let Err(first) = fs::copy(&source, dest_path) {
        let _ = Command::new(prefix.join("bin/darling"))
            .arg("--rootless")
            .arg("shutdown")
            .output();
        let holders = stop_prefix_holders(prefix);
        if !holders.is_empty() {
            println!(
                "PREFIX-STOP-HELD prefix={} pids={} (they still held the staged files after shutdown)",
                prefix.display(),
                holders.iter().map(|p| p.to_string()).collect::<Vec<_>>().join(",")
            );
        }
        return fs::copy(&source, dest_path)
            .map(|_| ())
            .with_context(|| {
                format!(
                    "installing {} -> {} (after a shutdown attempt and stopping {} prefix process(es); first error: {first})",
                    built.display(),
                    dest_path.display(),
                    holders.len()
                )
            });
    }
    Ok(())
}

fn runtime_fingerprint(prefix: &std::path::Path) -> String {
    const CANDIDATES: [&str; 7] = [
        "libexec/darling/usr/libexec/darling/mldr",
        "usr/libexec/darling/mldr",
        "libexec/darling/usr/lib/system/libsystem_kernel.dylib",
        "usr/lib/system/libsystem_kernel.dylib",
        "libexec/darling/usr/lib/dyld",
        "usr/lib/dyld",
        "libexec/darling/usr/bin/darlingserver",
    ];
    let mut parts: Vec<String> = Vec::new();
    let mut seen: Vec<String> = Vec::new();
    for rel in CANDIDATES {
        let p = prefix.join(rel);
        if !p.exists() {
            continue;
        }
        let sum = std::process::Command::new("sha256sum")
            .arg(&p)
            .output()
            .ok()
            .and_then(|o| String::from_utf8(o.stdout).ok())
            .and_then(|s| s.split_whitespace().next().map(|v| v.to_string()))
            .unwrap_or_else(|| "?".to_string());
        let short = sum.chars().take(12).collect::<String>();
        if seen.contains(&short) {
            continue;
        }
        seen.push(short.clone());
        let name = p
            .file_name()
            .and_then(|s| s.to_str())
            .unwrap_or("?")
            .to_string();
        parts.push(format!("{name}={short}"));
    }
    format!("prefix={} {}", prefix.display(), parts.join(" "))
}

fn run_one_workload(args: &VerdictArgs, tag: &str) -> Result<Verdict> {
    let Some(prefix) = args.prefix.as_ref() else {
        bail!("running a workload needs --prefix; use --log to judge a log that already exists");
    };
    let log = std::env::temp_dir().join(format!(
        "dwdiag-verdict-{}-{}{}.log",
        std::process::id(),
        args.mode,
        tag
    ));
    let _ = fs::remove_file(&log);
    // Self-attributing run: see runtime_fingerprint for the incident this exists to prevent.
    let fp = runtime_fingerprint(prefix);
    let env_log = log.with_extension("log.env");
    let _ = fs::write(&env_log, format!("[dwdiag-env {fp}]\n"));
    eprintln!("RUN-ENV {fp}");
    prefix_prereq_report(prefix);
    // The workload's OWN exit status is part of the observation: MEASURED, a workload that dies of SIGSEGV
    // (`EXITRC=139`) produces exactly the same evidence as a deadlock -- no result line -- and every
    // measurement drawn from "HANG" then chases a lock that does not exist. The status is printed by the
    // guest shell, so it is the guest's own answer, not the host launcher's.
    let cmd = format!(
        "{} {} {}; echo __DWDIAG_RC=$?",
        args.guest_command, args.mode, args.args
    );
    // RUN THE HARNESS IN ITS OWN SESSION, VIA THE setsid BINARY. MEASURED: the prefix teardown is group-directed
    // and killed the shell that asked for the run (rc=137 ~51 s in, twice). A pre_exec hook was tried first and
    // BROKE spawning outright (no log file was created at all), so the isolation is done here instead, where a
    // failure is visible: if setsid is missing we fall back to a direct spawn and say so.
    let use_setsid = std::path::Path::new("/usr/bin/setsid").exists()
        || std::path::Path::new("/bin/setsid").exists();
    let mut c = if use_setsid {
        let mut k = Command::new("setsid");
        k.arg(&args.boot_runner);
        k
    } else {
        Command::new(&args.boot_runner)
    };
    c.arg("--prefix")
        .arg(prefix)
        .arg("--wait")
        .arg(args.wait.to_string())
        .arg("--log")
        .arg(&log)
        // ONE argument: MEASURED, an empty value followed by the command made the harness see an extra positional and
        // exit instantly, which the verdict then reported as NO-RUN -- a tool defect that looked like a workload that
        // failed to start.
        .arg("--cmd")
        .arg(&cmd);
    for (k, v) in &args.env {
        c.arg("--env").arg(format!("{k}={v}"));
    }
    // THE GUEST LOG MUST BE CAPTURED BY IDENTITY, NOT BY LUCK. The verdict's own progress summary reads the guest
    // diagnostic log from MLDR_DIAG_LOG; nothing on this path set it, so every verdict printed last-guest=<none> and
    // the stage had to be found by hand in marker files. The harness writes the run's log to --log, and the loader
    // appends its diagnostics to whatever MLDR_DIAG_LOG names, so point both at the same file.
    c.env("MLDR_DIAG_LOG", &log);
    // A marker that can never appear: the harness exits non-zero, and the VERDICT below is ours, not its marker test.
    c.arg("--marker").arg("__dwdiag_never__");
    let out = c
        .output()
        .with_context(|| format!("running {}", args.boot_runner.display()))?;

    // The harness's own exit code is deliberately ignored: it reports whether its MARKERS appeared, which is not the
    // question here (MEASURED: a marker matching the workload's start line made a hang look like a pass).
    let _ = out;

    // Append the identity to the run's own log so the log alone answers "which build served this?".
    if log.exists() {
        use std::io::Write as _;
        if let Ok(mut f) = fs::OpenOptions::new().append(true).open(&log) {
            let _ = writeln!(f, "[dwdiag-env {fp}]");
        }
    }

    let text = read_log_lossy(&log).unwrap_or_default();
    // PREFER THE RESULT LINE, NOT THE FIRST LINE THAT MATCHES THE PREFIX. MEASURED: a mode whose informational
    // header and result line share the `RING_MACH_TEST mode=<M>` prefix (the fsview diagnostic does exactly that)
    // made this matcher read the HEADER, find no `pass=`, and report FAIL for a run whose workload printed
    // `pass=1` and exited 0. Every other mode prints one such line, so preferring a line that carries `pass=` --
    // and otherwise the LAST match, since a result is emitted at the end -- is strictly more honest.
    let (line, verdict, rc) = judge_run_log(&text, &args.mode);
    let denied = text
        .lines()
        .filter(|l| l.contains("rpc-socket-DENIED"))
        .count() as u64;
    let created = text
        .lines()
        .filter(|l| l.contains("rpc-socket] created") || l.contains("rpc-socket. created"))
        .count() as u64;
    let signal = rc
        .filter(|c| *c >= 128)
        .map(|c| signal_name(c - 128).to_string());

    let mut denial_call = None;
    let mut denial_location = None;
    if denied > 0 {
        if let Some(dl) = text.lines().find(|l| l.contains("rpc-socket-DENIED")) {
            let mut call = None;
            let mut delta = None;
            for f in dl.split_whitespace() {
                if let Some(v) = f.strip_prefix("call=") {
                    call = Some(v.to_string());
                } else if let Some(v) = f.strip_prefix("delta=") {
                    delta = Some(v.to_string());
                }
            }
            denial_call = call.clone();
            // The denial names the dependency AND its caller: `delta` is measured from `mach_driver_get_fd`, so the
            // location is one subtraction away -- which is exactly the derivation that used to be improvised by hand.
            if let (Some(d), Some(syms)) = (delta, args.guest_symbols.as_ref()) {
                if syms.exists() {
                    if let Ok(s) = load_symbols(syms) {
                        if let Some(base) = s
                            .iter()
                            .find(|s| symbol_name(&s.name) == "mach_driver_get_fd")
                        {
                            if let Ok(off) = parse_hex(&d) {
                                if let Some((i, o)) = locate(&s, base.addr + off) {
                                    denial_location =
                                        Some(format!("{} + 0x{:x}", symbol_name(&s[i].name), o));
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    Ok(Verdict {
        mode: args.mode.clone(),
        verdict,
        denied,
        created,
        line,
        denial_call,
        denial_location,
        log,
        rc,
        signal,
    })
}

/// Answer "what can I ask this prefix to do, and what will it print" from the FIXTURE ITSELF.
///
/// The mode names and the marker each mode emits are both already inside the workload binary as literals, so this reads
/// them out instead of keeping a list in the tool that could drift from the binary that runs. That drift is not
/// hypothetical: this session twice searched a source tree for the workload and found nothing, because the fixture is a
/// test asset installed into the prefix and its source lives in the canonical transport repository, not in every tree.
fn list_modes(args: &VerdictArgs) -> Result<ExitCode> {
    let Some(prefix) = args.prefix.as_ref() else {
        bail!("listing modes needs --prefix (the fixture lives inside a prefix)");
    };
    let host = prefix.join(args.guest_command.trim_start_matches('/'));
    if !host.is_file() {
        println!("MODES-FIXTURE absent {}", host.display());
        println!(
            "MODES: the workload fixture is a TEST ASSET; install it into the prefix (it is not part of the runtime install)"
        );
        return Ok(ExitCode::from(1));
    }
    println!("MODES-FIXTURE {}", host.display());
    let bytes = fs::read(&host).with_context(|| format!("reading {}", host.display()))?;
    // Literals of length >= 8 are printable ASCII runs; the workload's own strings are the contract.
    let mut markers: Vec<String> = Vec::new();
    let mut names: Vec<String> = Vec::new();
    let mut cur = String::new();
    for b in bytes.iter().chain(std::iter::once(&0u8)) {
        if (0x20..0x7f).contains(b) {
            cur.push(*b as char);
        } else {
            if cur.len() >= 8 && cur.contains("RING_MACH_TEST") {
                markers.push(cur.clone());
            }
            if (3..=20).contains(&cur.len())
                && cur.chars().next().is_some_and(|c| c.is_ascii_lowercase())
                && cur
                    .chars()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_')
            {
                names.push(cur.clone());
            }
            cur.clear();
        }
    }
    markers.sort();
    markers.dedup();
    names.sort();
    names.dedup();
    println!(
        "MODES-MARKERS {} (the line each workload prints; absence of it is the failure)",
        markers.len()
    );
    for m in &markers {
        println!("  {m}");
    }
    println!(
        "MODES-CANDIDATES {} (lowercase literals in the fixture; the mode is the first argument)",
        names.len()
    );
    println!("  {}", names.join(" "));
    println!(
        "MODES-USAGE dwdiag verdict --prefix <p> --mode <name> [--args '<args>'] [--wait <s>] [--repeat <n>]"
    );
    Ok(ExitCode::SUCCESS)
}

fn run_verdict(args: VerdictArgs) -> Result<ExitCode> {
    if args.list_modes {
        return list_modes(&args);
    }
    if args.mode.trim().is_empty() {
        // The tool must SAY what it needs instead of running a workload named "": list the modes it can see from the
        // fixture and stop, because that is the question an empty --mode actually asks.
        println!(
            "MODES: --mode is required; the fixture's own modes follow. Usage: --mode <name> [--args '<args>']"
        );
        let listed = list_modes(&args)?;
        let _ = listed;
        return Ok(ExitCode::from(2));
    }
    if let Some(path) = args.log.as_ref() {
        let text = read_log_lossy(path)
            .with_context(|| format!("reading the run log {}", path.display()))?;
        let (line, verdict, rc) = judge_run_log(&text, &args.mode);
        let denied = text.lines().filter(|l| l.contains("rpc-socket-DENIED")).count() as u64;
        let created = text
            .lines()
            .filter(|l| l.contains("rpc-socket] created") || l.contains("rpc-socket. created"))
            .count() as u64;
        println!("LOG={}", path.display());
        println!(
            "VERDICT mode={} verdict={} denied={} created={} rc={} :: {}",
            args.mode,
            verdict,
            denied,
            created,
            rc.map(|c| c.to_string()).unwrap_or_else(|| "?".into()),
            if line.is_empty() { "<no result line>".to_string() } else { line }
        );
        let p = summarize_progress(&text, &text, &args.mode);
        println!(
            "VERDICT-STAGE workload={} last-guest={} last-published-op={} last-served-op={} serviced={}",
            p.workload, p.last_guest, p.last_published_op, p.last_served_op, p.serviced
        );
        return Ok(ExitCode::from(u8::from(verdict != "PASS")));
    }
    let repeat = args.repeat.max(1);
    if repeat > 1 {
        let mut ok = 0usize;
        let mut verdicts: Vec<String> = Vec::with_capacity(repeat as usize);
        for i in 1..=repeat {
            let tag = format!("-r{i}");
            let v = run_one_workload(&args, &tag)?;
            // Stable, machine-readable: callers stop globbing a temp directory (MEASURED: a glob picked the
            // PREVIOUS run's log three times and the wrong process's identity twice).
            println!("LOG={}", v.log.display());
            println!(
                "VERDICT[{i}/{repeat}] mode={} verdict={} denied={} created={} rc={}",
                v.mode,
                v.verdict,
                v.denied,
                v.created,
                v.rc.map(|c| c.to_string()).unwrap_or_else(|| "?".into())
            );
            if v.ok() {
                ok += 1;
            }
            verdicts.push(v.verdict.clone());
            if !v.ok() {
                let text = read_log_lossy(&v.log).unwrap_or_default();
                // The loader appends its diagnostics to MLDR_DIAG_LOG, which this tool points at the SAME file the
                // harness writes, so the guest side and the harness side are one text. Reading a process environment
                // variable here instead was wrong twice over: this process never sets it in its own environment (only
                // in the child's), so every verdict printed last-guest=<none> no matter how much the guest logged.
                let guest = text.clone();
                // A BOOT THAT DIES OF A SIGNAL MUST EXPLAIN ITSELF IN THE SAME OUTPUT. MEASURED: the failing runs
                // printed VERDICT/VERDICT-STAGE and the crash had to be chased with a second command and a hand
                // computation. When the log carries a symbolized fault, print the fault and its caller chain here.
                if text.lines().any(|l| l.contains("[sigexc-fatal") && l.contains("rip=")) {
                    let _ = symbolize_guest_fault(&text, false);
                }
                let p = summarize_progress(&text, &guest, &v.mode);
                println!(
                    "VERDICT-STAGE workload={} last-guest={} last-published-op={} last-served-op={} serviced={}",
                    p.workload,
                    if p.last_guest.is_empty() {
                        "<none>"
                    } else {
                        &p.last_guest
                    },
                    if p.last_published_op.is_empty() {
                        "<none>"
                    } else {
                        &p.last_published_op
                    },
                    if p.last_served_op.is_empty() {
                        "<none>"
                    } else {
                        &p.last_served_op
                    },
                    p.serviced
                );
            }
        }
        let mut counts: Vec<(String, usize)> = Vec::new();
        for name in &verdicts {
            match counts.iter_mut().find(|(n, _)| n == name) {
                Some((_, c)) => *c += 1,
                None => counts.push((name.clone(), 1)),
            }
        }
        let dist = counts
            .iter()
            .map(|(n, c)| format!("{n}={c}"))
            .collect::<Vec<_>>()
            .join(" ");
        println!(
            "STABILITY mode={} runs={} pass={} stable={} distribution={}",
            args.mode,
            repeat,
            ok,
            if ok == repeat as usize { "yes" } else { "no" },
            dist
        );
        return Ok(if ok == repeat as usize {
            ExitCode::SUCCESS
        } else {
            ExitCode::from(1)
        });
    }
    let v = run_one_workload(&args, "")?;
    println!("LOG={}", v.log.display());
    if args.json {
        println!(
            "{{\"mode\":\"{}\",\"verdict\":\"{}\",\"denied\":{},\"created\":{},\"result\":\"{}\",\"denial_call\":{},\"denial_location\":{},\"rc\":{},\"signal\":{},\"log\":\"{}\"}}",
            v.mode,
            v.verdict,
            v.denied,
            v.created,
            jesc(&v.line),
            v.denial_call
                .as_ref()
                .map(|c| format!("\"{c}\""))
                .unwrap_or_else(|| "null".into()),
            v.denial_location
                .as_ref()
                .map(|c| format!("\"{c}\""))
                .unwrap_or_else(|| "null".into()),
            v.rc.map(|c| c.to_string()).unwrap_or_else(|| "null".into()),
            v.signal
                .as_ref()
                .map(|c| format!("\"{c}\""))
                .unwrap_or_else(|| "null".into()),
            jesc(&v.log.display().to_string())
        );
    } else {
        let extra = match (&v.denial_call, &v.denial_location) {
            (Some(c), Some(l)) => format!(" first-denial={c} caller={l}"),
            (Some(c), None) => format!(" first-denial={c}"),
            _ => String::new(),
        };
        println!(
            "VERDICT mode={} verdict={} denied={} created={} rc={}{} :: {}",
            v.mode,
            v.verdict,
            v.denied,
            v.created,
            v.rc.map(|c| c.to_string()).unwrap_or_else(|| "?".into()),
            extra,
            if v.line.is_empty() {
                "<no result line>"
            } else {
                &v.line
            }
        );
        // perf#30: a DENIAL is a migration signal even when the row passed. MEASURED: `sem_gap 5000 1` passed 3/3
        // while one run reported `denied=1`, and the tool printed the denial's call site only for non-PASS rows --
        // so the one fact needed to remove the remaining datagram dependency was hidden by a green verdict.
        if v.denied > 0 {
            let extra = match (&v.denial_call, &v.denial_location) {
                (Some(c), Some(l)) => format!(" first-denial={c} caller={l}"),
                (Some(c), None) => format!(" first-denial={c}"),
                _ => String::new(),
            };
            println!("DENIAL denied={}{} verdict={}", v.denied, extra, v.verdict);
        }
        // COMPOSED, not repeated: a non-PASS verdict is useless without the stage, and reading it out of two logs by
        // hand is exactly the work this tool exists to remove (doc section 230 -- the stall was found by grepping
        // `[mldr-ctl]` and `process-control-service` by hand, three runs in a row).
        if v.verdict != "PASS" {
            let text = read_log_lossy(&v.log).unwrap_or_default();
            // The loader appends its diagnostics to MLDR_DIAG_LOG, which this tool points at the SAME file the
            // harness writes, so the guest side and the harness side are one text. Reading this process's own
            // environment variable instead was wrong twice over: it is set only in the CHILD's environment
            // (MLDR_DIAG_LOG never appears here), so every verdict printed last-guest=<none> however much the
            // guest logged -- and this is the site the prefix path actually prints from.
            let guest = text.clone();
            // The SAME self-explanation as the other verdict path: a run that dies of a signal must name the fault and its
            // callers in this output, not only in a second command.
            if text.lines().any(|l| l.contains("[sigexc-fatal") && l.contains("rip=")) {
                let _ = symbolize_guest_fault(&text, false);
            }
            let p = summarize_progress(&text, &guest, &v.mode);
            println!(
                "VERDICT-STAGE workload={} last-guest={} last-published-op={} last-served-op={} serviced={}",
                p.workload,
                if p.last_guest.is_empty() {
                    "<none>"
                } else {
                    &p.last_guest
                },
                if p.last_published_op.is_empty() {
                    "<none>"
                } else {
                    &p.last_published_op
                },
                if p.last_served_op.is_empty() {
                    "<none>"
                } else {
                    &p.last_served_op
                },
                p.serviced
            );
        }
    }
    Ok(if v.ok() {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    })
}

#[derive(Args, Debug)]
pub struct SuiteArgs {
    /// Runtime prefix. Taken from DWDIAG_PREFIX when the flag is absent -- MEASURED FRICTION: `cycle` honoured the
    /// variable but `suite` demanded the flag, so a run started with the prefix exported died on clap's usage error
    /// and looked like a tool failure instead of a missing argument.
    #[arg(long)]
    prefix: Option<PathBuf>,
    /// Base watchdog in seconds; a mode whose arguments name a longer delay gets that added.
    #[arg(long, default_value_t = 60)]
    wait_base: u64,
    #[arg(long, value_parser = parse_kv)]
    env: Vec<(String, String)>,
    /// Fail the run if any row created a per-thread socket.
    #[arg(long)]
    require_zero_creations: bool,
    #[arg(long, default_value = "scripts/darling-boot-run.sh")]
    boot_runner: PathBuf,
    #[arg(long)]
    guest_symbols: Option<PathBuf>,
    #[arg(long)]
    json: bool,
    /// `MODE [ARGS]` chunks separated by `::`.
    #[arg(last = true, required = true)]
    modes: Vec<String>,
    /// Retry a row that did not pass, once, announcing both verdicts. Row-level flake has decided suite verdicts
    /// here (measured), and a retry that is printed is evidence, not silence.
    #[arg(long, default_value_t = true, action = clap::ArgAction::Set)]
    retry_failed_rows: bool,
}

fn split_chunks(modes: &[String]) -> Vec<String> {
    let joined = modes.join(" ");
    joined
        .split("::")
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect()
}

fn run_suite(args: SuiteArgs) -> Result<ExitCode> {
    let chunks = split_chunks(&args.modes);
    let mut rows: Vec<Verdict> = Vec::new();
    let mut failures = 0usize;
    for chunk in &chunks {
        let mut it = chunk.split_whitespace();
        let mode = it.next().unwrap_or_default().to_string();
        let margs = it.collect::<Vec<_>>().join(" ");
        let mut extra = 0u64;
        for a in margs.split_whitespace() {
            if let Ok(n) = a.parse::<u64>() {
                if n > 1000 {
                    extra = extra.max(n / 1000);
                }
            }
        }
        let va = VerdictArgs {
            prefix: Some(required_path("--prefix", args.prefix.clone(), "DWDIAG_PREFIX")?),
            mode: mode.clone(),
            args: margs.clone(),
            wait: args.wait_base + extra,
            env: args.env.clone(),
            boot_runner: args.boot_runner.clone(),
    guest_command: "/private/var/tmp/ring_mach_msg_test".to_string(),
            guest_symbols: args.guest_symbols.clone(),
            repeat: 1,
            list_modes: false,
            json: false,
            log: None,
        };
        let mut v = run_one_workload(&va, "")?;
        // perf#30: a row that is not PASS is retried ONCE, and both verdicts are printed. MEASURED: two acceptance
        // rows failed in one suite run (`sem_gap 5000 1` HANG, `basic 20` NO-RUN) and then passed 3/3 and 4/4 when
        // run individually -- i.e. the row-level flake, not the workload, decided the suite verdict. The retry is
        // announced, never silent: hiding the first verdict would be the same defect as a verdict that cannot fail.
        if !v.ok() && args.retry_failed_rows {
            let first = v.verdict.clone();
            let v2 = run_one_workload(&va, "-retry")?;
            if !args.json {
                println!(
                    "ROW-RETRY mode={} first={} retry={}",
                    va.mode, first, v2.verdict
                );
            }
            if v2.ok() {
                v = v2;
            } else {
                v = v2;
            }
        }
        if !v.ok() {
            failures += 1;
        }
        if args.require_zero_creations && v.created != 0 {
            failures += 1;
        }
        // A ROW KILLED BY THE WATCHDOG IS NOT A WORKLOAD FAILURE. MEASURED: the suite's watchdog is the base wait
        // alone, the basic mode needs about 200 s of work, and its first attempt died by SIGTERM at that boundary
        // with no result line -- printed as a plain failed row, which is exactly the confusion this tool exists to
        // remove (only the automatic retry let the suite pass). A signal death with no result line is the harness
        // stopping a run that was still working, so it is named as such and the wait used is shown next to it.
        let watchdog = v.line.is_empty()
            && (v.verdict.contains("watchdog")
                || v.verdict.contains("SIGTERM")
                || v.verdict.contains("SIGKILL"));
        let shown_verdict = if watchdog { "WATCHDOG?".to_string() } else { v.verdict.clone() };
        if !args.json {
            println!(
                "{:<28} {:<9} {:<8} {:<8} {}",
                format!("{} {}", v.mode, margs).trim(),
                shown_verdict,
                v.denied,
                v.created,
                if watchdog {
                    format!("<no result line; killed at the wait boundary with wait={}s -- raise --wait-base>", va.wait)
                } else if v.line.is_empty() {
                    "<no result line>".to_string()
                } else {
                    v.line.clone()
                }
            );
        }
        rows.push(v);
    }
    let pass = failures == 0;
    if args.json {
        let rows: Vec<String> = rows
            .iter()
            .map(|v| {
                format!(
                    "{{\"mode\":\"{}\",\"verdict\":\"{}\",\"denied\":{},\"created\":{}}}",
                    v.mode, v.verdict, v.denied, v.created
                )
            })
            .collect();
        println!(
            "{{\"rows\":[{}],\"failures\":{},\"require_zero_creations\":{},\"verdict\":\"{}\"}}",
            rows.join(","),
            failures,
            args.require_zero_creations,
            if pass { "PASS" } else { "FAIL" }
        );
    } else {
        println!(
            "SUITE rows={} failures={} require_zero_creations={}",
            rows.len(),
            failures,
            args.require_zero_creations as u8
        );
        println!("SUITE-VERDICT {}", if pass { "PASS" } else { "FAIL" });
    }
    Ok(if pass {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    })
}

// ----------------------------------------------------------------------------------------------------------------
// `progress`: WHERE a run stopped. MEASURED need (doc sections 227/230): every stall this cycle cost a manual grep
// over a boot log and a guest loader log, and the answer was always one of a handful of shapes -- the workload's own
// result line is absent, the guest loader's last `[mldr-ctl]` line says which op it published or which stage it
// reached, and the server's last `process-control-service` line says which op it actually serviced. That difference
// (published vs serviced) is the whole diagnosis, so it belongs in the tool and not in a shell pipeline.

#[derive(Args, Debug)]
pub struct ProgressArgs {
    /// The run log the boot harness wrote (`--log`).
    #[arg(long)]
    log: PathBuf,
    /// The guest loader diagnostic file (`MLDR_DIAG_LOG`), when the run enabled one.
    #[arg(long)]
    guest_log: Option<PathBuf>,
    /// A REGRESSION TRIPWIRE, not a proof: fail (exit 2) when fewer plane publishes were seen than this. MEASURED
    /// need: two broken boots showed `plane-publishes=6` where a healthy boot shows 40+, and noticing that required
    /// remembering the healthy number -- the tool must say it instead of leaving it to the reader.
    #[arg(long, default_value_t = 0)]
    min_plane_publishes: u64,
    /// The workload's mode, used to look for its own machine-readable result line.
    #[arg(long, default_value = "")]
    mode: String,
    #[arg(long)]
    json: bool,
    /// The server's OWN log. MEASURED: the server's stderr never appears in the run log -- it goes to the prefix's
    /// private/var/log/dserver.log -- so several of this session's conclusions were drawn from an absence that meant
    /// "not captured" rather than "not produced". Reading it here makes "the server sent it" and "the guest received
    /// it" two facts in one report instead of a guess.
    /// Server log to search. Left unset, the prefix is taken from the run's own identity line (or DWDIAG_PREFIX),
    /// so a run served by another prefix cannot have its evidence read from the wrong one.
    #[arg(long)]
    server_log: Option<PathBuf>,
}

#[derive(Debug, Default)]
pub struct Progress {
    pub workload: String,
    pub result: String,
    pub last_guest: String,
    pub last_published_op: String,
    pub last_served_op: String,
    pub serviced: u64,
    pub denied: u64,
    pub created: u64,
    pub first_denial_call: String,
}

fn scalar(prefix: &str) -> Option<String> {
    // "op=5 pid=917234" -> the value of `op`, for any whitespace-separated KEY=VALUE in an unstructured line.
    // A bare key with no `=` (`after-seed`) is returned as-is, because that IS the stage name.
    if !prefix.contains('=') {
        return Some(prefix.to_string());
    }
    prefix.split_once('=').map(|(_, v)| v.to_string())
}

/// The workload's OWN result line for a mode: the matching line that carries `pass=`, else the LAST matching line.
/// MEASURED: the fsview diagnostic prints an informational header and a result line that both carry the mode
/// prefix. The verdict rule read the header and reported FAIL for a run whose workload printed pass=1 and exited
/// 0, and the progress summary printed the header as PROGRESS-RESULT. One selection rule, used by both.
fn workload_result_line<'a>(text: &'a str, mode: &str) -> Option<&'a str> {
    let prefix = if mode.is_empty() {
        "RING_MACH_TEST mode=".to_string()
    } else {
        format!("RING_MACH_TEST mode={mode} ")
    };
    let matches: Vec<&str> = text.lines().filter(|l| l.contains(&prefix)).collect();
    matches
        .iter()
        .rev()
        .find(|l| l.contains("pass="))
        .or_else(|| matches.last())
        .copied()
}

fn summarize_progress(run: &str, guest: &str, mode: &str) -> Progress {
    let mut p = Progress::default();
    if let Some(l) = workload_result_line(run, mode) {
        p.workload = "present".to_string();
        p.result = l.trim().to_string();
    } else if run.contains("RING_MACH_TEST mode=") {
        p.workload = "other-mode".to_string();
    } else {
        p.workload = "absent".to_string();
    }
    p.denied = run
        .lines()
        .filter(|l| l.contains("rpc-socket-DENIED"))
        .count() as u64;
    p.created = run
        .lines()
        .filter(|l| l.contains("rpc-socket] created") || l.contains("rpc-socket. created"))
        .count() as u64;
    if let Some(dl) = run.lines().find(|l| l.contains("rpc-socket-DENIED")) {
        for f in dl.split_whitespace() {
            if let Some(v) = f.strip_prefix("call=") {
                p.first_denial_call = v.to_string();
            }
        }
    }
    // The server side: last serviced op, and the count. `process-control-service ... op=N ...` is the line that proves
    // the server DID see and answer the request the guest may still be waiting on.
    for l in run
        .lines()
        .filter(|l| l.contains("process-control-service"))
    {
        p.serviced += 1;
        for f in l.split_whitespace() {
            if let Some(v) = f.strip_prefix("op=") {
                p.last_served_op = v.to_string();
            }
        }
    }
    // The guest side: the LAST `[mldr-ctl]` line that says what is being waited for. MEASURED DEFECT, fixed here: the
    // first version tested `head == "seq"` against a line whose first token is `seq=7`, so the stage rule could never
    // fire and the tool reported a `planeloop BEGIN` from early in the boot as the "last" line -- an instrument that
    // silently does nothing is indistinguishable from no instrument, which is the class this project keeps recording.
    // Lines are therefore classified by the key BEFORE `=`, and a priority decides which one survives: a stage line
    // beats a spin, and a timeout beats everything (it is the current, terminal state).
    let mut best: (u8, String) = (0, String::new());
    for l in guest.lines().filter(|l| l.contains("[mldr-ctl]")) {
        let rest = l.split("[mldr-ctl]").nth(1).unwrap_or("").trim();
        let head = rest.split_whitespace().next().unwrap_or("");
        let kind = head.split('=').next().unwrap_or("");
        let mut pid = String::new();
        for w in rest.split_whitespace() {
            if let Some(v) = w.strip_prefix("pid=") {
                pid = v.to_string();
            }
        }
        let (prio, rendered) = match kind {
            "request" => {
                let mut op = String::new();
                for w in rest.split_whitespace() {
                    if let Some(v) = w.strip_prefix("op=") {
                        op = v.to_string();
                    }
                }
                p.last_published_op = op.clone();
                (1u8, format!("published op={op} pid={pid}"))
            }
            "planeloop" => {
                let mut op = String::new();
                let mut mine = String::new();
                for w in rest.split_whitespace() {
                    if let Some(v) = w.strip_prefix("op=") {
                        op = v.to_string();
                    } else if let Some(v) = w.strip_prefix("mine=") {
                        mine = v.to_string();
                    }
                }
                (2, format!("waiting op={op} mine={mine} pid={pid}"))
            }
            "iter" => (
                0,
                format!(
                    "spin {}",
                    rest.split_whitespace()
                        .take(3)
                        .collect::<Vec<_>>()
                        .join(" ")
                ),
            ),
            "plane-request" if rest.contains("TIMEOUT") => (9, format!("TIMEOUT {}", rest)),
            "seq" => {
                // `seq=N after-<stage> pid=... image=...` -- the bootstrap stage names, which is what a stall is read
                // against when the workload never speaks.
                let stage = rest
                    .split_whitespace()
                    .find(|w| w.starts_with("after-") || w.starts_with("before-"));
                match stage {
                    Some(st) => (5, format!("{st} pid={pid}")),
                    None => (1, format!("seq {}", rest)),
                }
            }
            _ => (0, rest.to_string()),
        };
        if prio >= best.0 {
            best = (prio, rendered);
        }
    }
    // FALLBACK TO THE REAL MARKER FAMILY. The classifier above only reads `[mldr-ctl]` lines, but the loader and the
    // kernel emit bracketed marks of other families -- [fd-courier-conn], [plane-doorbell], [dring-adopt], [open-*],
    // [pc-*], [drop-op-*], [rmmt]. MEASURED: a run with 1418 lines of real guest activity printed last-guest=<none>,
    // because none of that activity is `[mldr-ctl]`; the stage then had to be found by hand. When the structured rule
    // finds nothing, name the LAST bracketed line instead of nothing: an unnamed stall is indistinguishable from an
    // absent guest.
    if best.1.is_empty() {
        if let Some(l) = guest
            .lines()
            .rev()
            .find(|l| l.trim_start().starts_with('[') && l.contains(']'))
        {
            let mut s = l.trim().to_string();
            s.truncate(96);
            best = (1, s);
        }
    }
    p.last_guest = best.1;
    let _ = scalar("");
    p
}

#[derive(clap::Args, Debug)]
pub struct WitnessArgs {
    /// The run log to census. Defaults to the newest `dwdiag` verdict log in the temp directory.
    #[arg(long)]
    log: Option<PathBuf>,
    #[arg(long)]
    json: bool,
}

// THE REGISTRY. One entry per instrument this project ships, with the line it writes and why it exists. Adding an
// instrument without adding it here stops the census from being a census, so the list is the point of the command:
// `witness` answers "which of these spoke", and names the ones that did not.
const INSTRUMENTS: &[(&str, &str, &str)] = &[
    (
        "dthread-mask",
        r"^\[dthread-mask ",
        "loader: the kernel signal mask of a NEWLY created guest thread -- a header that inherits a blocked SIGSEGV makes the fault undeliverable there",
    ),
    (
        "sigexc-deliver",
        r"^\[sigexc-deliver ",
        "guest: the FINAL dispatch of a delivered signal -- the point every delivery path reaches, unlike a probe in the function prologue",
    ),
    (
        "fault",
        r"^\[fault sig=11 ",
        "guest: the FAULT ITSELF -- si_addr (the address that faulted) and gregs.rip (where execution was), printed inside the handler the host disposition points at",
    ),
    (
        "segvdisp",
        r"^\[segvdisp ",
        "guest: the HOST disposition of SIGSEGV read with a raw rt_sigaction query next to the point where the workload stops",
    ),
    (
        "wait4",
        r"^\[wait4 ",
        "guest: the pid the host reaped and its RAW host status -- a signaled child is signum|0x80, and the pid names the dying process",
    ),
    (
        "guest-handler",
        r"^\[guest-handler ",
        "guest: the kernel ENTERED the wrapper that runs the guest's own handler -- for a fatal SIGSEGV this fires when the fault reached the guest",
    ),
    (
        "sigsegv-mask",
        r"^\[sigsegv-mask ",
        "guest: mask transitions carrying Darwin's SIGSEGV bit -- a block with no matching unblock means the forced default action",
    ),
    (
        "sigexc-setup",
        r"^\[sigexc-setup ",
        "guest: the ONE place that installs Darling's delivery handler for every signal -- did it run for this process",
    ),
    (
        "setrestart",
        r"^\[setrestart ",
        "guest: the install of Darling's delivery handler for a guest-requested signal -- ret is the kernel's answer",
    ),
    (
        "sigact",
        r"^\[sigact ",
        "guest: every transition of SIGSEGV's disposition on the HOST (req=0 is SIG_DFL, req=1 is SIG_IGN) -- names who removed the handler",
    ),
    (
        "native-exit",
        r"^\[native-exit ",
        "guest: the boundary where the guest hands ITS OWN exit status to the host -- a 139 here would mean the guest decided it",
    ),
    (
        "sigexc-in",
        r"^\[sigexc-in ",
        "guest: a GUEST linux signal number arriving in the guest's own signal machinery (sigexc_handler)",
    ),
    (
        "sem-site",
        r"^SEM-SITE ",
        "guest: who calls the semaphore family, with which name/address, and from which thread",
    ),
    (
        "iter-marks",
        r"^ITER [0-9]+ ",
        "guest workload: per-iteration progress, names the iteration that stopped",
    ),
    (
        "stall-dump",
        r"stall-dump idle_ms=",
        "server: parked threads, their calls, and their wait-timer state",
    ),
    (
        "ring-dump",
        r"dtape\.ering (dump|seq=)",
        "server: the in-memory event ring dumped when the counters stop",
    ),
    (
        "plane-refuse",
        r"plane-refuse",
        "server: a plane request refused at the op that refused it",
    ),
    (
        "dtape-msgq",
        r"dtape\.msgq event=",
        "server: msgq park/send/post/wake order",
    ),
    (
        "dtape-timer",
        r"dtape\.wait_timer event=",
        "server: wait-timer prepare/expire/unblock",
    ),
    (
        "rpc-begin",
        r"rpc\.[a-z_0-9]+\.begin",
        "server: an RPC request the server began",
    ),
    (
        "rpc-reply",
        r"rpc\.[a-z_0-9]+\.reply",
        "server: an RPC reply the server enqueued (begin without reply is a stall)",
    ),
    (
        "crash",
        r"dserver-CRASH",
        "server: the crash probe, with its fault address and stack walk",
    ),
    (
        "workload-stall",
        r"RING_MACH_TEST_STALL",
        "guest workload: its own watchdog fired",
    ),
    (
        "execpath-after",
        r"after-execpath",
        "server: the post-exec completion-store barrier",
    ),
    // Added 2026-09-27 with the per-thread-socket removal and the diagnostics that closed the silent-death
    // investigation. Each one is an instrument that was added to the tree and therefore has to be counted here, or
    // `witness` reports a live instrument as silent (the failure this registry exists to prevent).
    (
        "sigexc",
        r"\[sigexc-(fatal|default) sig=",
        "guest: the fault translator reporting a fatal/returned raw signal",
    ),
    (
        "plane-slow",
        r"\[plane-slow op=",
        "guest: a process-control request the server did not complete in time",
    ),
    (
        "modrefs",
        r"\[modrefs-(entry|exit) ",
        "guest: the mach_port_mod_refs trap around its impl and its exit code",
    ),
    (
        "allocprobe",
        r"\[allocprobe\]",
        "guest: an allocation-path probe taken while a lock-free path was suspected",
    ),
    (
        "ring-trace-gen",
        r"RING_TRACE gen (ENTER|EXIT) callnum=",
        "guest: the generated-call trampoline's enter/exit pair",
    ),
    (
        "iter-drop",
        r"ITER [0-9]+ tid=[0-9]+ drop_",
        "guest workload: which drop path an iteration took",
    ),
    (
        "rpc-socket-denied",
        r"\[rpc-socket-DENIED\] ",
        "guest: a caller that has no lane and no plane op, and the call it is (the removal's own instrument)",
    ),
    (
        "checkout-path",
        r"\[checkout-path\] ",
        "guest: a thread-exit checkout that could not be published, with the state that prevented it",
    ),
    (
        "checkin-path",
        r"\[checkin-path\] ",
        "guest: a checkin that could not be published, with the state that prevented it",
    ),
    (
        "release-drops-pending",
        r"\[release-drops-pending\] site=",
        "guest/server: a completed request whose slot was released while still pending, by site",
    ),
];

/// Count each registered instrument's lines in `text`, and keep one sample per instrument for the human to read.
pub fn witness_census(text: &str) -> Vec<(String, u64, String)> {
    INSTRUMENTS
        .iter()
        .map(|(name, pat, _why)| {
            let re = Regex::new(pat).expect("instrument pattern is a literal of this file");
            let mut count = 0u64;
            let mut sample = String::new();
            for line in text.lines() {
                if re.is_match(line) {
                    count += 1;
                    if sample.is_empty() {
                        let mut t: String = line.chars().take(140).collect();
                        if line.chars().count() > 140 {
                            t.push('…');
                        }
                        sample = t;
                    }
                }
            }
            ((*name).to_string(), count, sample)
        })
        .collect()
}

fn newest_verdict_log() -> Option<PathBuf> {
    let dir = std::env::temp_dir();
    let mut best: Option<(std::time::SystemTime, PathBuf)> = None;
    for entry in fs::read_dir(&dir).ok()?.flatten() {
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if !name.starts_with("dwdiag-verdict-") || !name.ends_with(".log") {
            continue;
        }
        let Ok(md) = entry.metadata() else { continue };
        let Ok(mtime) = md.modified() else { continue };
        if best.as_ref().map(|(t, _)| mtime > *t).unwrap_or(true) {
            best = Some((mtime, entry.path()));
        }
    }
    best.map(|(_, p)| p)
}

fn run_witness(args: WitnessArgs) -> Result<ExitCode> {
    let log = match args.log {
        Some(p) => p,
        None => newest_verdict_log()
            .context("no `dwdiag-verdict-*.log` in the temp directory; pass --log")?,
    };
    let text = read_log_lossy(&log).with_context(|| format!("reading {}", log.display()))?;
    let census = witness_census(&text);
    let fired: Vec<_> = census.iter().filter(|(_, c, _)| *c > 0).collect();
    let silent: Vec<_> = census
        .iter()
        .filter(|(_, c, _)| *c == 0)
        .map(|(n, _, _)| n.clone())
        .collect();
    if args.json {
        let mut obj = String::from("{\"log\":\"");
        obj.push_str(&jesc(&log.display().to_string()));
        obj.push_str("\",\"instruments\":[");
        for (i, (name, count, sample)) in census.iter().enumerate() {
            if i > 0 {
                obj.push(',');
            }
            obj.push_str(&format!(
                "{{\"name\":\"{}\",\"count\":{},\"sample\":\"{}\"}}",
                jesc(name),
                count,
                jesc(sample)
            ));
        }
        obj.push_str("]}");
        println!("{obj}");
    } else {
        println!(
            "WITNESS log={} instruments={} fired={}",
            log.display(),
            census.len(),
            fired.len()
        );
        for (name, count, sample) in &census {
            if *count > 0 {
                println!("  {name:<14} {count:>7}  {sample}");
            }
        }
        println!(
            "WITNESS-SILENT {}",
            if silent.is_empty() {
                "<none>".to_string()
            } else {
                silent.join(",")
            }
        );
    }
    Ok(ExitCode::SUCCESS)
}

fn run_progress(args: ProgressArgs) -> Result<ExitCode> {
    // Name the log that was actually read (user directive: never withhold a fact that changes the reading). MEASURED
    // need: a `--log`-less invocation silently picked a different run's log and the wake census described THAT run --
    // a reader comparing two runs would have attributed the numbers to the wrong one.
    eprintln!("PROGRESS-LOG {}", args.log.display());
    let run = read_log_lossy(&args.log).unwrap_or_default();
    let guest = args
        .guest_log
        .as_ref()
        .map(|p| read_log_lossy(p).unwrap_or_default())
        .unwrap_or_default();
    let p = summarize_progress(&run, &guest, &args.mode);
    // Proactive stop reason (user directive): a CRASHED run has no workload result line, so every summary field is
    // empty and the output used to read `workload=absent` -- which says nothing about WHY. MEASURED need: a
    // darlingserver abort in the plane pass produced exactly that, and the crash was only found by grepping the log
    // by hand. The crash line plus the first panic-backtrace frames are printed here, with the command that decodes
    // them, so the reason is never withheld from the next reader.
    let crash_line = run.lines().find(|l| l.contains("dserver-CRASH")).map(|l| {
        l.split("dserver-CRASH")
            .last()
            .unwrap_or(l)
            .trim()
            .to_string()
    });
    let mut crash_frames: Vec<String> = Vec::new();
    if crash_line.is_some() {
        for line in run.lines() {
            let l = line.trim();
            if l.starts_with("darlingserver(+0x") && crash_frames.len() < 4 {
                crash_frames.push(l.to_string());
            }
        }
    }
    // Wake-channel summary (user directive: the reading violation A turns on must be one command away). A plane
    // publish is either woken by the process doorbell or only found later by the server's bounded poll, and the
    // loader's instrument now names which. Counting OCCURRENCES, not lines, is deliberate: the older instrument ended
    // its record with a literal backslash-n, so several records shared one line and a line-based count undercounts
    // exactly the path this is meant to expose.
    let (mut wake_doorbell, mut wake_none, mut wake_unknown) = (0u64, 0u64, 0u64);
    {
        let mut idx = 0usize;
        while let Some(rel) = run[idx..].find("[plane-wake]") {
            let at = idx + rel;
            idx = at + 1;
            let seg = &run[at..(at + 160).min(run.len())];
            // TOKEN equality, never a substring test: the retired label was `via=doorbell-or-server-poll`, which
            // CONTAINS `via=doorbell`, so a substring classifier reported 38 doorbell wakes for a run whose channel
            // was in fact never recorded -- a verdict wrong in the most dangerous direction. Extracting the token
            // makes the old label land in `unknown`, which is the truth about it.
            let token = seg.find("via=").map(|v| {
                let rest = &seg[v + 4..];
                let end = rest.find(|c: char| c.is_whitespace()).unwrap_or(rest.len());
                &rest[..end]
            });
            match token {
                Some("doorbell") => wake_doorbell += 1,
                Some("none") => wake_none += 1,
                _ => wake_unknown += 1,
            }
        }
    }
    if args.json {
        println!(
            "{{\"workload\":\"{}\",\"result\":\"{}\",\"last_guest\":\"{}\",\"last_published_op\":\"{}\",\"last_served_op\":\"{}\",\"serviced\":{},\"denied\":{},\"created\":{},\"first_denial_call\":\"{}\"}}",
            jesc(&p.workload),
            jesc(&p.result),
            jesc(&p.last_guest),
            jesc(&p.last_published_op),
            jesc(&p.last_served_op),
            p.serviced,
            p.denied,
            p.created,
            jesc(&p.first_denial_call)
        );
        if let Some(cl) = &crash_line {
            println!(
                "{{\"crash\":\"{}\",\"frames\":[{}]}}",
                jesc(cl),
                crash_frames
                    .iter()
                    .map(|f| format!("\"{}\"", jesc(f)))
                    .collect::<Vec<_>>()
                    .join(",")
            );
        }
    } else {
        println!(
            "PROGRESS workload={} last-guest={} last-published-op={} last-served-op={} serviced={} denied={} created={} first-denial={}",
            p.workload,
            if p.last_guest.is_empty() {
                "<none>"
            } else {
                &p.last_guest
            },
            if p.last_published_op.is_empty() {
                "<none>"
            } else {
                &p.last_published_op
            },
            if p.last_served_op.is_empty() {
                "<none>"
            } else {
                &p.last_served_op
            },
            p.serviced,
            p.denied,
            p.created,
            if p.first_denial_call.is_empty() {
                "<none>"
            } else {
                &p.first_denial_call
            }
        );
        if !p.result.is_empty() {
            println!("PROGRESS-RESULT {}", p.result);
        }
        if wake_doorbell + wake_none + wake_unknown > 0 {
            let total_publishes = wake_doorbell + wake_none + wake_unknown;
            println!(
                "WAKES plane-publishes={} doorbell={} none={} unknown={}",
                total_publishes, wake_doorbell, wake_none, wake_unknown
            );
            if args.min_plane_publishes > 0 && total_publishes < args.min_plane_publishes {
                println!(
                    "WAKES-REGRESSION only {total_publishes} plane publishes, at least {} expected: a boot that stops early looks exactly like this",
                    args.min_plane_publishes
                );
            }
            if wake_none > 0 {
                println!(
                    "WAKES-VERDICT the bounded poll is LOAD-BEARING: {wake_none} publish(es) had no doorbell to ring"
                );
            } else if wake_unknown == 0 {
                println!(
                    "WAKES-VERDICT every publish rang the doorbell: the poll is not what makes progress"
                );
            } else {
                println!(
                    "WAKES-VERDICT {wake_unknown} record(s) predate the channel-aware instrument; re-run to judge"
                );
            }
        }
        // A receive that missed its token means a bundle was consumed by ANOTHER consumer or never arrived -- the exact
        // mechanism that broke two boots in this session (a doorbell envelope adopted instead of stored, while the lane
        // attach still waited for it by token). The line existed in the log; nothing surfaced it.
        let misses: Vec<&str> = run
            .lines()
            .filter(|l| {
                l.contains("fd-courier-recv") && l.contains("MISS")
                    || l.contains("[fd-courier-miss]")
            })
            .collect();
        if !misses.is_empty() {
            println!(
                "COURIER-MISSES {} record(s); first: {}",
                misses.len(),
                misses[0].trim()
            );
        } else {
            println!(
                "COURIER-MISSES none visible in this log (the receive-side instrument is env-gated; absence here is not evidence that no bundle was lost)"
            );
        }
        // SERVER IDENTITY (user directive: the tool must not let an assumption stand in for evidence). MEASURED need:
        // a whole series of experiments appeared to have "no effect" after a deploy because the RUNS WERE SERVED BY A
        // STALE darlingserver -- the project's own documented trap -- and nothing in the report said which binary was
        // actually running. This prints, for every live darlingserver, its executable, its sha256 and its start time,
        // and compares that hash with the deployed prefix binary: MATCH is the only state in which "the deploy took
        // effect" is a fact.
        {
            let deployed: u64 = fs::read("/tmp/dr-on-matched/bin/darlingserver")
                .map(|b| b.len() as u64)
                .unwrap_or(0);
            let mut found = 0usize;
            if let Ok(entries) = fs::read_dir("/proc") {
                for e in entries.flatten() {
                    let name = e.file_name().to_string_lossy().to_string();
                    if name.is_empty() || !name.chars().all(|c| c.is_ascii_digit()) {
                        continue;
                    }
                    let exe = match fs::read_link(format!("/proc/{name}/exe")) {
                        Ok(x) => x,
                        Err(_) => continue,
                    };
                    let exe_s = exe.to_string_lossy().to_string();
                    if !exe_s.contains("darlingserver") {
                        continue;
                    }
                    found += 1;
                    let size = fs::metadata(&exe).map(|m| m.len()).unwrap_or(0);
                    // Start time: /proc/<pid>/stat field 22 (starttime in clock ticks) plus btime is more than this
                    // needs -- the uptime-relative value is enough to say WHICH incarnation is running.
                    let stat = std::fs::read_to_string(format!("/proc/{name}/stat")).unwrap_or_default();
                    let start_ticks = stat.rsplit(')').next().and_then(|rest| {
                        rest.split_whitespace()
                            .nth(19)
                            .and_then(|v| v.parse::<u64>().ok())
                    });
                    let uptime = std::fs::read_to_string("/proc/uptime").unwrap_or_default();
                    let up: f64 = uptime
                        .split_whitespace()
                        .next()
                        .and_then(|v| v.parse().ok())
                        .unwrap_or(0.0);
                    let started_ago = start_ticks
                        .map(|ticks| up - (ticks as f64 / 100.0))
                        .unwrap_or(-1.0);
                    let sum = std::process::Command::new("sha256sum")
                        .arg(&exe)
                        .output()
                        .ok()
                        .and_then(|o| String::from_utf8(o.stdout).ok())
                        .and_then(|s| {
                            s.split_whitespace()
                                .next()
                                .map(|h| h.chars().take(16).collect::<String>())
                        })
                        .unwrap_or_default();
                    let verdict = if size != deployed {
                        format!("SIZE-MISMATCH (deployed is {deployed} bytes, running is {size})")
                    } else {
                        "same size as the deployed binary (hash not compared byte-wise)".to_string()
                    };
                    println!(
                        "SERVER-IDENTITY pid={name} exe={exe_s} sha256={sum} started_ago={started_ago:.1}s -- {verdict}"
                    );
                }
            }
            if found == 0 {
                println!(
                    "SERVER-IDENTITY no darlingserver process is running (the harness may stop it between runs)"
                );
            }
        }
        // SERVER SIDE, from its own log (see the option's comment). Counts only: the two files have different clocks,
        // so an interleaved ordering would be a fabricated one. Presence plus count is what decides the question.
        {
            // The prefix comes from the run's own identity line when the caller did not name a log, so a run served
            // by another prefix cannot have its server evidence read from the wrong one (see resolve_prefix).
            /* THE PREFIX COMES FROM THE LOG BEING READ, BEFORE ANY DEFAULT (dar-4cp9). MEASURED: a boot-fail run
             * of /tmp/r1-repro-prefix was reported with SERVER-LOG-PATH /tmp/dr-on-matched/private/var/log/... and
             * a doorbell count of 19228 taken from THAT prefix's server log -- a number about a different prefix,
             * presented next to this run's report. The run's own identity line carries the resolved prefix, so read
             * it from there first; name the source of whatever path is used, and refuse to present a count whose
             * file belongs to another prefix. */
            let mut prefix_from_log: Option<PathBuf> = None;
            for line in run.lines() {
                let mut rest = line;
                while let Some(i) = rest.find("prefix=") {
                    rest = &rest[i + "prefix=".len()..];
                    let end = rest.find(|c: char| c.is_whitespace() || c == ']').unwrap_or(rest.len());
                    if end > 0 {
                        let cand = PathBuf::from(&rest[..end]);
                        if cand.is_absolute() {
                            prefix_from_log = Some(cand);
                        }
                    }
                    rest = &rest[end..];
                }
            }
            let (server_log_path, server_log_source) = match args.server_log.clone() {
                Some(p) => (p, "explicit --server-log"),
                None => match prefix_from_log.clone().or_else(|| resolve_prefix(None, None)) {
                    Some(p) => (p.join("private/var/log/dserver.log"), "the run's own prefix line"),
                    None => (
                        PathBuf::from("/tmp/dr-on-matched").join("private/var/log/dserver.log"),
                        "DEFAULT -- no prefix in the log; counts below may belong to another prefix",
                    ),
                },
            };
            eprintln!("SERVER-LOG-PATH {} (from {})", server_log_path.display(), server_log_source);
            let server_text = read_log_lossy(&server_log_path).unwrap_or_default();
            if server_text.is_empty() {
                println!(
                    "SERVER-LOG unreadable or empty ({}) -- server-side facts are NOT included in this report",
                    server_log_path.display()
                );
            } else {
                let sent = server_text
                    .lines()
                    .filter(|l| l.contains("plane-doorbell-sent"))
                    .count();
                let guest_timeouts = run
                    .lines()
                    .filter(|l| l.contains("drain attempts=") && l.contains("adopted=0"))
                    .count();
                println!(
                    "SERVER-SENT plane-doorbell-sent={sent} (from {})",
                    server_log_path.display()
                );
                if sent > 0 && guest_timeouts > 0 {
                    println!(
                        "SERVER-GUEST-SPLIT the server sent the doorbell {sent} time(s) while {guest_timeouts} guest drain window(s) expired empty: the descriptor is being SENT but not RECEIVED (look at the guest's receive path, not at the sender)"
                    );
                } else if sent == 0 {
                    println!(
                        "SERVER-GUEST-SPLIT the server did not send anything: the send path never ran"
                    );
                }
            }
        }
        // ORDERED TIMELINE of the three events that decide violation A's ordering. MEASURED need: a run showed 14
        // doorbell ADOPTIONS and 44 publishes that still reported `db=-1`, and no reading said WHICH came first --
        // counts cannot express an ordering, and the ordering is the whole remaining question. The events are
        // printed in log order with their position, so "the first N publishes precede the first adoption" is a
        // sentence the tool says instead of one the reader infers.
        {
            let mut events: Vec<(usize, String)> = Vec::new();
            for (n, line) in run.lines().enumerate() {
                // Only a REAL adoption is an ADOPT event. The drain-outcome line carries the same tag and says
                // `adopted=0`, and counting it as an adoption produced the false verdict "every publish followed the
                // first adoption" for a run whose own drain lines said the opposite. Same lesson as the earlier
                // substring classifier: match the VALUE.
                if line.contains("[plane-doorbell]")
                    && line.contains("adopted=1")
                    && !line.contains("drain")
                {
                    events.push((
                        n,
                        format!(
                            "ADOPT   {}",
                            line.trim().chars().take(90).collect::<String>()
                        ),
                    ));
                } else if line.contains("[plane-wake]") {
                    let seg = line;
                    let via = seg
                        .find("via=")
                        .map(|v| {
                            let r = &seg[v + 4..];
                            let e = r.find(|c: char| c.is_whitespace()).unwrap_or(r.len());
                            &r[..e]
                        })
                        .unwrap_or("?");
                    let db = seg
                        .find("db=")
                        .map(|v| {
                            let r = &seg[v + 3..];
                            let e = r.find(|c: char| c.is_whitespace()).unwrap_or(r.len());
                            &r[..e]
                        })
                        .unwrap_or("?");
                    events.push((n, format!("PUBLISH via={via} db={db}")));
                } else if line.contains("attach-rc") {
                    let wake = line
                        .find("wake=")
                        .map(|v| {
                            let r = &line[v + 5..];
                            let e = r.find(|c: char| c.is_whitespace()).unwrap_or(r.len());
                            &r[..e]
                        })
                        .unwrap_or("?");
                    events.push((n, format!("ATTACH  wake={wake}")));
                }
            }
            if !events.is_empty() {
                println!(
                    "TIMELINE first {} of {} event(s):",
                    events.len().min(12),
                    events.len()
                );
                for (n, e) in events.iter().take(12) {
                    println!("  line {n:>5}  {e}");
                }
                let first_adopt = events
                    .iter()
                    .find(|(_, e)| e.starts_with("ADOPT"))
                    .map(|(n, _)| *n);
                let publishes_before = match first_adopt {
                    Some(a) => events
                        .iter()
                        .filter(|(n, e)| *n < a && e.starts_with("PUBLISH"))
                        .count(),
                    None => events
                        .iter()
                        .filter(|(_, e)| e.starts_with("PUBLISH"))
                        .count(),
                };
                match first_adopt {
                    Some(_) if publishes_before == 0 => {
                        println!("TIMELINE-VERDICT every publish followed the first adoption")
                    }
                    Some(_) => println!(
                        "TIMELINE-VERDICT {publishes_before} publish(es) PRECEDED the first adoption: the ordering the doorbell needs is not yet in place"
                    ),
                    None => println!("TIMELINE-VERDICT no adoption in this log at all"),
                }
            }
        }
        if let Some(cl) = &crash_line {
            println!("STOP-REASON crash: {cl}");
            for f in &crash_frames {
                println!("  {f}");
            }
            println!(
                "  decode: dwdiag crash --binary <server binary> --log {}",
                args.log.display()
            );
        }
    }
    // The exit code is a QUESTION ("did the workload speak?"), not a judgement: absent means the caller must look at
    // `last-guest`/`last-served-op`, present means the run got far enough to be judged on the result line itself.
    Ok(if p.workload == "present" {
        ExitCode::SUCCESS
    } else {
        ExitCode::from(1)
    })
}

// ----------------------------------------------------------------------------------------------------------------

#[derive(clap::Args, Debug)]
pub struct DenialsArgs {
    #[arg(long)]
    log: PathBuf,
    /// Kernel image whose symbol `mach_driver_get_fd` is the base for every denial's `delta`.
    #[arg(long)]
    kernel: Option<PathBuf>,
    #[arg(long)]
    json: bool,
}

/// The denials of one run, as a table, with the caller resolved and the surrounding lifecycle context attached.
fn run_denials(args: DenialsArgs) -> Result<ExitCode> {
    let text = read_log_lossy(&args.log).with_context(|| format!("reading {}", args.log.display()))?;
    let syms = match &args.kernel {
        Some(k) if k.exists() => Some(load_symbols(k)?),
        Some(k) => {
            crate::say(&format!("DENIALS: kernel image {} does not exist; callers will not be symbolized", k.display()));
            None
        }
        None => None,
    };
    let base = syms
        .as_ref()
        .and_then(|s| s.iter().find(|s| symbol_name(&s.name) == "mach_driver_get_fd"))
        .map(|s| s.addr);
    // The context that has decided every question in this stage: was this process's transport rebound, and did the plane
    // or the urgent pool refuse it first?
    let mut rebinds: Vec<(u64, usize)> = Vec::new();
    let mut refusals: Vec<(usize, String)> = Vec::new();
    for (i, line) in text.lines().enumerate() {
        if let Some(rest) = line.split("postfork-child rebind pid=").nth(1) {
            if let Ok(pid) = rest.split_whitespace().next().unwrap_or("").parse::<u64>() {
                rebinds.push((pid, i));
            }
        }
        if line.contains("[plane-refuse]") || line.contains("[urgent-refuse]") {
            refusals.push((i, line.trim().to_string()));
        }
    }
    let mut seen = std::collections::HashSet::new();
    let mut rows = 0usize;
    crate::say(&format!("DENIALS log={} lines={}", args.log.display(), text.lines().count()));
    for (i, line) in text.lines().enumerate() {
        let Some(pos) = line.find("rpc-socket-DENIED") else { continue };
        let fields: Vec<&str> = line[pos..].split_whitespace().collect();
        let get = |k: &str| fields.iter().find_map(|f| f.strip_prefix(k)).map(|v| v.to_string());
        let pid: u64 = get("pid=").and_then(|v| v.parse().ok()).unwrap_or(0);
        let call = get("call=").unwrap_or_default();
        if !seen.insert((pid, call.clone())) {
            continue;
        }
        let delta = get("delta=").unwrap_or_default();
        let location = match (base, parse_hex(&delta)) {
            (Some(b), Ok(d)) => syms
                .as_ref()
                .and_then(|s| locate(s, b + d))
                .map(|(idx, off)| format!("{} + 0x{:x}", symbol_name(&syms.as_ref().unwrap()[idx].name), off))
                .unwrap_or_else(|| "NOT-IN-IMAGE".to_string()),
            _ => "UNRESOLVED (pass --kernel)".to_string(),
        };
        let rebound = rebinds.iter().find(|(p, _)| *p == pid).map(|(_, r)| if *r < i { "before" } else { "after" });
        let refusal = refusals.iter().rev().find(|(ri, _)| *ri < i).map(|(_, l)| l.clone()).unwrap_or_default();
        rows += 1;
        crate::say(&format!(
            "DENIAL pid={pid} call={call} image={} delta={delta} at={location} rebind={} refusal={}",
            get("image=").unwrap_or_default(),
            rebound.unwrap_or("none"),
            if refusal.is_empty() { "-".to_string() } else { refusal }
        ));
    }
    crate::say(&format!("DENIALS-DONE rows={rows} rebinds={}", rebinds.len()));
    Ok(if rows == 0 { ExitCode::SUCCESS } else { ExitCode::from(1) })
}

#[derive(clap::Args, Debug)]
pub struct TraceArgs {
    #[arg(long)]
    log: PathBuf,
    /// The pid or tid to follow. Lines naming it, without needing a word-boundary guess by the caller.
    #[arg(long)]
    pid: u64,
    /// Also show lines that name no pid but carry these tokens (refusals, attach, courier, checkin...).
    #[arg(long = "also", num_args = 0.., default_values_t = Vec::<String>::new())]
    also: Vec<String>,
    /// Only the last N matching lines.
    #[arg(long)]
    tail: Option<usize>,
    /// Also search this other log (typically the prefix's private/var/log/dserver.log) for the same pid, so one command
    /// answers "what did the guest do" AND "what did the server do" without a second hand-written grep.
    #[arg(long)]
    server: Option<PathBuf>,
}

fn run_trace(args: TraceArgs) -> Result<ExitCode> {
    let text = read_log_lossy(&args.log).with_context(|| format!("reading {}", args.log.display()))?;
    let pid = args.pid.to_string();
    let pid_patterns = [format!("pid={pid}"), format!("tid={pid}"), format!("pid={pid} "), format!("tid={pid} ")];
    let defaults = ["plane-refuse", "urgent-refuse", "dring-attach", "courier-", "checkin", "release-drops-pending"];
    if let Some(server_path) = &args.server {
        match read_log_lossy(server_path) {
            Ok(server_text) => {
                let matches: Vec<&str> = server_text
                    .lines()
                    .filter(|l| l.contains(&format!("pid={pid}")) || l.contains(&format!("tid={pid}")))
                    .collect();
                crate::say(&format!(
                    "TRACE-SERVER {} lines={} matches={}",
                    server_path.display(),
                    server_text.lines().count(),
                    matches.len()
                ));
                for l in matches.iter().rev().take(12).rev() {
                    crate::say(&format!("  S {}", l.trim()));
                }
            }
            Err(_) => crate::say(&format!("TRACE-SERVER {} (unreadable)", server_path.display())),
        }
    }
    let mut hits: Vec<(usize, &str)> = Vec::new();
    for (i, line) in text.lines().enumerate() {
        let names_pid = pid_patterns.iter().any(|p| line.contains(p.as_str()));
        let carries_token = args.also.iter().any(|a| line.contains(a.as_str()))
            || (args.also.is_empty() && defaults.iter().any(|d| line.contains(d)));
        if names_pid || carries_token {
            hits.push((i, line));
        }
    }
    let total = hits.len();
    let shown: Vec<&(usize, &str)> = match args.tail {
        Some(n) if n < total => hits.iter().skip(total - n).collect(),
        _ => hits.iter().collect(),
    };
    crate::say(&format!("TRACE log={} pid={} matched={} shown={}", args.log.display(), args.pid, total, shown.len()));
    for (i, l) in shown {
        crate::say(&format!("{i}: {}", l.trim()));
    }
    crate::say(&format!("TRACE-DONE matched={total}"));
    Ok(if total == 0 { ExitCode::from(1) } else { ExitCode::SUCCESS })
}

pub fn dispatch(cmd: DiagCommand) -> Result<ExitCode> {
    match cmd {
        DiagCommand::Source(a) => run_source_check(a),
        DiagCommand::Watch(a) => run_watch(a),
        DiagCommand::Build(a) => run_build(a),
        DiagCommand::Deploy(a) => run_deploy(a),
        DiagCommand::Cycle(a) => run_cycle(a),
        DiagCommand::Symbolize(a) => run_symbolize(a),
        DiagCommand::Crash(a) => run_crash(a),
        DiagCommand::Verdict(a) => run_verdict(a),
        DiagCommand::Suite(a) => run_suite(a),
        DiagCommand::Progress(a) => run_progress(a),
        DiagCommand::Courier(a) => run_courier(a),
        DiagCommand::Witness(a) => run_witness(a),
        DiagCommand::Prefix(a) => run_prefix(a),
        DiagCommand::Denials(a) => run_denials(a),
        DiagCommand::Trace(a) => run_trace(a),
    }
}

/// The durable-artifact gate, as ONE supported command instead of a hand-rolled script per attempt.
///
/// MEASURED motivation: proving that the CLEAN-BUILT artifacts work took three ad-hoc scripts, each of which grew its own
/// defects (a stuck `$?` after a pipe, a lost output stream, artifacts installed from the session prefix by hand). The
/// steps are the same every time -- bootstrap a prefix with a named profile, install the built artifacts with a sha256
/// check per copy, install the test assets, run a workload, read the census -- so they belong here, where the transcript,
/// the log path and the stage announcements already are.
///
/// Every step prints a `PREFIX-STAGE` line BEFORE it runs. That is not decoration: a gate that was killed at a wait
/// boundary once produced no output at all, and the stage line is what turns "it went silent" into "it died at boot".
#[derive(clap::Args, Debug)]
pub struct PrefixArgs {
    #[arg(long)]
    prefix: PathBuf,
    /// Bootstrap profile for `west test --bootstrap-runtime-profile`. Omit to use the prefix as it is.
    #[arg(long)]
    bootstrap_profile: Option<String>,
    /// `BUILT=DESTINATION` pairs for the artifacts under test, each sha256-checked after installation. DESTINATION is
    /// inside the prefix unless it is absolute.
    #[arg(long = "install", value_parser = parse_kv, num_args = 0..)]
    install: Vec<(String, String)>,
    /// `SOURCE=DESTINATION` pairs for TEST ASSETS, which are not part of the runtime install (the workload fixture is
    /// one, and its absence is what made an earlier run report workload=absent).
    #[arg(long = "asset", value_parser = parse_kv, num_args = 0..)]
    asset: Vec<(String, String)>,
    /// `NAME=BUILT` for a runtime component, expanded to EVERY copy the prefix layout needs, because the guest root is
    /// `libexec/darling` and a component installed only at the host-visible path is one the guest cannot open (MEASURED:
    /// `Cannot open /usr/lib/dyld` after exactly that mistake). The layout is knowledge this tool must hold rather than a
    /// list every caller retypes: darlingserver -> bin/darlingserver; mldr -> usr/libexec/darling/mldr and its libexec
    /// mirror; libsystem_kernel -> usr/lib/system/... and its mirror; dyld -> usr/lib/dyld and its mirror.
    #[arg(long = "artifact", value_parser = parse_kv, num_args = 0..)]
    artifact: Vec<(String, String)>,
    /// `KEY=VALUE` environment for the harness, and through it for the launcher, the server and the guest -- the same
    /// shape as `verdict --env`. MEASURED need: a gate that needs an environment must not be reachable only by exporting
    /// it in the caller's shell; one attempt at enabling the server-side plane log that way did not arrive at all.
    #[arg(long = "env", value_parser = parse_kv, num_args = 0..)]
    env: Vec<(String, String)>,
    /// Workload to run when the installation succeeded; omitted stops after the installation.
    #[arg(long, default_value = "")]
    mode: String,
    #[arg(long, default_value = "")]
    args: String,
    /// The workload's path AS THE GUEST SEES IT. MEASURED: the guest's /usr/bin resolves through /Volumes/SystemRoot
    /// to the HOST's /usr/bin, so a fixture installed at <prefix>/usr/bin is invisible to it and the run dies with
    /// "/usr/bin/<fixture>: not found" (rc=127) -- which reads as a broken workload rather than a wrong location. The
    /// guest-visible, writable place is /private/var/tmp, so that is the default here.
    #[arg(long, default_value = "/private/var/tmp/ring_mach_msg_test")]
    guest_command: String,
    #[arg(long, default_value_t = 260)]
    wait: u64,
    #[arg(long)]
    json: bool,
}

/// The installed copy must equal the built file, and the check has to be on the DESTINATION: `install` was measured to
/// succeed while the deployed file was the stale one, and a deploy that is not verified is a claim about the wrong
/// binary. Uses the host `sha256sum` rather than pulling a hash crate in for one comparison.
fn sha256_of(path: &Path) -> Result<String> {
    let out = Command::new("sha256sum")
        .arg(path)
        .output()
        .with_context(|| format!("hashing {}", path.display()))?;
    let text = String::from_utf8_lossy(&out.stdout);
    Ok(text
        .split_whitespace()
        .next()
        .unwrap_or_default()
        .to_string())
}

fn run_prefix(args: PrefixArgs) -> Result<ExitCode> {
    let prefix = &args.prefix;
    if let Some(profile) = &args.bootstrap_profile {
        println!(
            "PREFIX-STAGE bootstrap profile={profile} prefix={}",
            prefix.display()
        );
        if prefix.exists() {
            // `west test` refuses a non-empty prefix by design, and a half-installed prefix would make every later
            // step lie about what it measured: start from nothing, and say so.
            println!("PREFIX-STAGE clearing {}", prefix.display());
            fs::remove_dir_all(prefix).with_context(|| format!("clearing {}", prefix.display()))?;
        }
        fs::create_dir_all(prefix)?;
        let status = Command::new("mise")
            .args(["run", "west", "test", "--prefix"])
            .arg(prefix)
            .args(["--bootstrap-runtime-profile", profile])
            .status()
            .context("running the bootstrap")?;
        println!(
            "PREFIX-STAGE bootstrap done rc={}",
            status.code().unwrap_or(-1)
        );
        if !status.success() {
            println!("PREFIX: bootstrap FAILED; nothing was installed and no workload was run");
            return Ok(ExitCode::from(3));
        }
    }
    let mut installed = 0usize;
    let layout: &[(&str, &[&str])] = &[
        ("darlingserver", &["bin/darlingserver"]),
        ("mldr", &["usr/libexec/darling/mldr", "libexec/darling/usr/libexec/darling/mldr"]),
        (
            "libsystem_kernel",
            &["usr/lib/system/libsystem_kernel.dylib", "libexec/darling/usr/lib/system/libsystem_kernel.dylib"],
        ),
        ("dyld", &["usr/lib/dyld", "libexec/darling/usr/lib/dyld"]),
        // MEASURED FRICTION: the guest's pthread library is a component a diagnosis regularly needs to deploy (the
        // creation path lives in it), and without an entry here the cycle refused it as "unknown component" while the
        // prefix could install it perfectly well -- a dead end in the tool, not in the runtime.
        ("libsystem_pthread.dylib", &[
            "usr/lib/system/libsystem_pthread.dylib",
            "libexec/darling/usr/lib/system/libsystem_pthread.dylib",
        ]),
    ];
    let mut pairs: Vec<(String, String)> = args.install.clone();
    for (name, built) in &args.artifact {
        let destinations = layout
            .iter()
            .find(|(n, _)| n == name)
            .map(|(_, d)| *d)
            .with_context(|| {
                format!(
                    "--artifact {name}: unknown component; known: {}",
                    layout.iter().map(|(n, _)| *n).collect::<Vec<_>>().join(", ")
                )
            })?;
        for dest in destinations {
            pairs.push((built.clone(), (*dest).to_string()));
        }
    }
    for (built, dest) in &pairs {
        let dest_path = if PathBuf::from(dest).is_absolute() {
            PathBuf::from(dest)
        } else {
            prefix.join(dest)
        };
        println!("PREFIX-STAGE install {built} -> {}", dest_path.display());
        let source = PathBuf::from(built);
        if !source.is_file() {
            println!("PREFIX-INSTALL MISSING-BUILD-ARTIFACT {built}");
            continue;
        }
        // ETXTBSY: A RUNNING darlingserver CANNOT BE OVERWRITTEN. MEASURED: the install of bin/darlingserver failed
        // with the tool exiting 1 right after printing the install line, which reads as "the run refused" rather than
        // "the server is still up". The documented rule is to stop the server first, so do exactly that here and
        // retry once -- the alternative is every caller remembering it.
        install_artifact_into_prefix(Path::new(built), &dest_path, prefix)?;
        let a = sha256_of(&source)?;
        let b = sha256_of(&dest_path)?;
        println!(
            "PREFIX-INSTALL {} {} {}",
            if a == b { "MATCH" } else { "MISMATCH" },
            &a[..16.min(a.len())],
            dest_path.display()
        );
        if a != b {
            return Ok(ExitCode::from(3));
        }
        installed += 1;
    }
    for (src, dest) in &args.asset {
        let dest_path = if PathBuf::from(dest).is_absolute() {
            PathBuf::from(dest)
        } else {
            prefix.join(dest)
        };
        println!("PREFIX-STAGE asset {src} -> {}", dest_path.display());
        // A FRESH PREFIX DOES NOT HAVE usr/bin, AND THE COPY'S ENOENT NAMES NEITHER THE MISSING DIRECTORY NOR THE
        // DESTINATION. MEASURED: that cost a full debugging cycle -- the failure looked like a missing SOURCE file
        // while the source was present and only its parent directory was absent. Create the parent and name the
        // destination in the error.
        if let Some(parent) = dest_path.parent() {
            fs::create_dir_all(parent)
                .with_context(|| format!("creating asset destination directory {}", parent.display()))?;
        }
        fs::copy(src, &dest_path)
            .with_context(|| format!("installing asset {src} -> {}", dest_path.display()))?;
    }
    println!(
        "PREFIX-INSTALLED artifacts={installed} assets={}",
        args.asset.len()
    );
    // ADVISORY GUEST-VIEW CHECK, printed BEFORE the workload runs. The guest's /usr/bin resolves through
    // /Volumes/SystemRoot to the HOST's /usr/bin, so a fixture copied into <prefix>/usr/bin is not the one the
    // workload opens. MEASURED: a gate whose fixture was never installed via --asset ends with workload=absent and
    // says nothing about why, and that shape cost this work repeated cycles. This line is what makes the difference
    // visible at the top of the run instead of after it.
    /* THE FIXTURE IS PART OF THE MEASUREMENT, SO REFRESH IT FROM THE BUILD TREE (dar-4cp9). MEASURED trap this
     * closes: the guest fixture is read from the prefix, and a run of an OLD copy is indistinguishable from a run
     * of the new one -- this session hit it exactly, running `forkexec` against a fixture installed hours earlier
     * that did not contain the mode at all, and the workload answered `unknown_mode` for a mode that was in the
     * source. The build tree is the only source of truth here (DWDIAG_BUILD), the copy is verified by sha256 like
     * every other artifact, and a missing source is reported rather than silently skipped. */
    {
        let base = Path::new(&args.guest_command)
            .file_name()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_else(|| "ring_mach_msg_test".to_string());
        let build_root = std::env::var("DWDIAG_BUILD")
            .ok()
            .filter(|s| !s.is_empty())
            .map(PathBuf::from);
        let src = match build_root {
            Some(b) => b.join("src/tools").join(&base),
            None => PathBuf::from("/nonexistent-no-build-tree").join(&base),
        };
        let dst = prefix.join(args.guest_command.trim_start_matches('/'));
        if src.is_file() {
            if let (Ok(a), _) = (sha256_of(&src), ()) {
                let same = sha256_of(&dst).map(|b| b == a).unwrap_or(false);
                if !same {
                    match install_artifact_into_prefix(src.as_path(), &dst, prefix) {
                        Ok(()) => {
                            let b = sha256_of(&dst).unwrap_or_default();
                            println!(
                                "FIXTURE-INSTALL {} {} {}",
                                if b == a { "MATCH" } else { "MISMATCH" },
                                &a[..16.min(a.len())],
                                dst.display()
                            );
                        }
                        Err(e) => println!("FIXTURE-INSTALL FAILED {e}"),
                    }
                } else {
                    println!("FIXTURE-INSTALL MATCH {} (already current) {}", &a[..16.min(a.len())], dst.display());
                }
            }
        } else {
            println!("FIXTURE-INSTALL MISSING-BUILD-ARTIFACT {}", src.display());
        }
    }
    let host_view = prefix.join(args.guest_command.trim_start_matches('/'));
    println!(
        "ASSET-CHECK guest={} host={} host-present={} asset-installs={}",
        args.guest_command,
        host_view.display(),
        host_view.is_file(),
        args.asset.len()
    );
    if args.mode.trim().is_empty() {
        println!("PREFIX-STAGE: no --mode given, stopping after installation");
        return Ok(ExitCode::SUCCESS);
    }
    // A server log that ACCUMULATES across runs cannot answer "what did the server do during THIS run", which is the
    // question every server-side probe comes down to. Record each log's line count before and after, so the slice is
    // named and any later query can be scoped to it.
    let server_logs = ["private/var/log/dserver.log", "private/var/log/dserver-diag.log"];
    let mut server_before: Vec<(String, u64)> = Vec::new();
    for rel in server_logs {
        let p = prefix.join(rel);
        let n = read_log_lossy(&p).map(|s| s.lines().count() as u64).unwrap_or(0);
        server_before.push((p.display().to_string(), n));
    }
    println!(
        "PREFIX-STAGE workload mode={} args={:?} wait={}",
        args.mode, args.args, args.wait
    );
    let va = VerdictArgs {
        prefix: Some(prefix.clone()),
        mode: args.mode.clone(),
        args: args.args.clone(),
        wait: args.wait,
        env: args.env.clone(),
        boot_runner: PathBuf::from("scripts/darling-boot-run.sh"),
        guest_command: args.guest_command.clone(),
        guest_symbols: None,
        repeat: 1,
        list_modes: false,
        json: args.json,
        log: None,
    };
    let code = run_verdict(va);
    for (path, n) in server_before {
        let after = std::fs::read_to_string(&path).map(|s| s.lines().count() as u64).unwrap_or(0);
        crate::say(&format!("SERVER-LOG-SLICE {path} lines={n}:{after} (this run)"));
    }
    code
}

#[derive(clap::Subcommand, Debug)]
pub enum DiagCommand {
    /// Watch a running guest's kernel signal dispositions (SigCgt/SigBlk) against the run log's size.
    Watch(WatchArgs),
    /// Report whether a build tree actually compiles given source files, and refuse when it does not.
    Source(SourceArgs),
    /// Build the runtime pair the rule requires (libsystem_kernel and the dyld image TOGETHER) and check that the
    /// instrument strings a caller expects are present in the built artifact.
    Build(BuildArgs),
    /// Build, deploy and run ONE workload, then report which instruments fired and which stayed silent: the four
    /// steps this stage repeats, with the prefix, the build tree and the artifact pair taken from the environment.
    /// Deploy the complete component set from ONE build tree into a prefix, verifying every copy by sha256.
    Deploy(DeployArgs),
    Cycle(CycleArgs),
    /// Resolve a reported offset to `symbol + offset` for any image (guest dylib or server binary).
    Symbolize(SymbolizeArgs),
    /// Turn a `dserver-CRASH` line into a location, a stack walk and the disassembly around the fault.
    Crash(CrashArgs),
    /// Run ONE guest workload and judge it by its own machine-readable line; absence of that line is FAIL/HANG.
    Verdict(VerdictArgs),
    /// Run a SET of guest workloads and print one table with the counters that decide acceptance.
    Suite(SuiteArgs),
    /// Say WHERE a finished run stopped: the workload's own line, the guest loader's last stage, and the last plane op
    /// the server actually serviced. Published-vs-serviced is the diagnosis; this prints both instead of grepping.
    Progress(ProgressArgs),
    /// Count the packets per TRANSPORT for a run log, and judge the AF_UNIX endpoint's purity. Rows with no
    /// instrument behind them are printed UNMEASURED, never as zero: a census that cannot see a transport must say so.
    Courier(CourierArgs),
    /// Report which INSTRUMENTS fired in a run log, and which stayed SILENT. A registered instrument with zero hits is
    /// the failure this exists to catch: a guard that silently does nothing cannot be told from no guard at all, and a
    /// hand-written grep over one log finds a line but cannot tell you which of the other instruments never spoke.
    Witness(WitnessArgs),
    /// The chronologically ordered story of ONE process in a run log: every line that names its pid or tid, plus the
    /// plane/urgent refusals around it. This is the question of this whole stage -- "what happened to this child?" --
    /// and it was reconstructed by hand with grep/sed/python six times, each time risking the wrong slice of a
    /// two-million-line log.
    Trace(TraceArgs),
    /// One row per DISTINCT denial in a run log, with the caller SYMBOLIZED and the post-fork context next to it.
    ///
    /// This is the recurring question of this whole stage, assembled by hand five times: which call, which pid, which
    /// caller, and was that process's transport rebound before the denial? The `delta` on a denial line is measured from
    /// the symbol `mach_driver_get_fd` in the KERNEL image (not dyld, not the server binary), which is a trap that has
    /// already produced one wrong attribution; passing `--kernel` resolves it here instead of by hand.
    Denials(DenialsArgs),
    /// Prove a PREFIX rather than a working tree: bootstrap it, install the built artifacts with a sha256 check each,
    /// install the test assets, then run ONE workload on it. Prints a PREFIX-STAGE line before every step, so a gate that
    /// dies part way says where it died instead of going silent.
    Prefix(PrefixArgs),
}

// A tiny helper used by the table so a caller can see the set in a stable order under --json too.
#[allow(dead_code)]
fn btree_from(pairs: Vec<(String, String)>) -> BTreeMap<String, String> {
    pairs.into_iter().collect()
}

#[derive(clap::Args, Debug)]
pub struct CourierArgs {
    /// Run log. Defaults to the newest `dwdiag-verdict-*.log`.
    #[arg(long)]
    log: Option<PathBuf>,
    #[arg(long)]
    json: bool,
    /// The DEPLOYED artifact whose instruments this census relies on (default: the live prefix's loader). A silent
    /// instrument and an instrument that sent nothing look identical in a log; the artifact decides which.
    /// Deployed artifacts whose instruments this census relies on; defaults to the live prefix's loader and kernel
    /// image when none is given.
    #[arg(long)]
    artifact: Vec<PathBuf>,
}

// THE TRANSPORT CENSUS. One row per transport, with the log token that counts it and its source instrument. A row whose
// instrument does not exist in the tree yet is printed UNMEASURED with the reason -- the alternative (a zero) is a
// number nobody measured, which is the failure mode this project keeps recording.
struct TransportRow {
    name: &'static str,
    /// Log patterns whose matching lines count this transport.
    patterns: &'static [&'static str],
    /// None = no instrument exists for this row today.
    instrument: Option<&'static str>,
    /// A token whose presence in the DEPLOYED artifact makes this row's count evidence rather than silence.
    proof_token: &'static str,
    /// A row that MUST be zero for the courier-purity requirement.
    must_be_zero: bool,
    /// The row's instrument is OPT-IN (an env-gated trace): its silence then means the trace was off, not that no
    /// traffic flowed. The count is reported as UNMEASURED rather than 0 (directive section 12: no fabricated zeros).
    opt_in_instrument: bool,
}

const TRANSPORTS: &[TransportRow] = &[
    TransportRow {
        name: "SPSC Ring (per-thread lane, ordinary calls)",
        patterns: &[r"RING_TRACE gen ENTER callnum="],
        instrument: Some("RING_TRACE gen ENTER (only under DARLING_GUEST_RING_TRACE=1)"),
        proof_token: "RING_TRACE gen ENTER callnum=",
        must_be_zero: false,
        opt_in_instrument: true,
    },
    TransportRow {
        name: "duplex Ring/mailbox (caller-S2C, OOL)",
        patterns: &[r"RING_MACHMSG_PUBLISH", r"dtape\.msgq event="],
        instrument: Some("guest RING_MACHMSG_PUBLISH / server dtape.msgq"),
        proof_token: "RING_MACHMSG_PUBLISH",
        must_be_zero: false,
        opt_in_instrument: true,
    },
    TransportRow {
        name: "process management plane (shared page, slot)",
        patterns: &[r"\[plane-", r"\[release-drops-pending\]"],
        instrument: Some("plane-* lines (partial: only named paths print)"),
        proof_token: "[plane-",
        must_be_zero: false,
        opt_in_instrument: false,
    },
    TransportRow {
        name: "urgent shared plane (interrupt/sigprocess)",
        patterns: &[r"urgent-service"],
        instrument: Some("urgent-service (server: one line per serviced urgent slot)"),
        proof_token: "urgent-service",
        must_be_zero: false,
        opt_in_instrument: false,
    },
    TransportRow {
        name: "SCM_RIGHTS courier (fd-bearing packets)",
        patterns: &[r"\[afunix-send\] .*scm=1", r"\[courier-send\] .*scm=1"],
        instrument: Some("afunix-send + courier-send (scm=1)"),
        proof_token: "[courier-send]",
        must_be_zero: false,
        opt_in_instrument: false,
    },
    TransportRow {
        name: "legacy ordinary AF_UNIX (semantic, no fd)",
        patterns: &[r"\[afunix-send\] .*scm=0", r"\[courier-send\] .*scm=0"],
        instrument: Some("afunix-send + courier-send (scm=0)"),
        proof_token: "[afunix-send]",
        must_be_zero: true,
        opt_in_instrument: false,
    },
    TransportRow {
        name: "zero-fd control/wake packets on AF_UNIX",
        patterns: &[r"\[afunix-wake\]"],
        instrument: Some(
            "afunix-wake (gone: the plane wake is now a non-packet; [plane-wake] records it)",
        ),
        proof_token: "[plane-wake]",
        must_be_zero: true,
        opt_in_instrument: false,
    },
];

fn run_courier(args: CourierArgs) -> Result<ExitCode> {
    let log = match args.log {
        Some(p) => p,
        None => newest_verdict_log()
            .context("no `dwdiag-verdict-*.log` in the temp directory; pass --log")?,
    };
    let text = read_log_lossy(&log).with_context(|| format!("read {}", log.display()))?;
    println!(
        "COURIER log={} lines={}",
        log.display(),
        text.lines().count()
    );
    // The ARTIFACT decides whether a zero is evidence: a token that is not in the deployed binary means the row was
    // never measured, however quiet the log is. (A silent probe is not evidence -- this project's own rule.)
    let default_artifacts = vec![
        PathBuf::from("/tmp/dr-on-matched/libexec/darling/usr/libexec/darling/mldr"),
        PathBuf::from("/tmp/dr-on-matched/libexec/darling/usr/lib/system/libsystem_kernel.dylib"),
        // The SERVER carries the plane's own instruments (plane-wake, courier-send, urgent-service), so a census of
        // those rows cannot be judged without it: a zero whose instrument is missing from this binary is not
        // evidence. MEASURED need: the urgent plane's instrument lives here and was not in the default set.
        PathBuf::from("/tmp/dr-on-matched/bin/darlingserver"),
    ];
    let artifacts = if args.artifact.is_empty() {
        &default_artifacts
    } else {
        &args.artifact
    };
    let mut artifact_blobs: Vec<Vec<u8>> = Vec::new();
    for a in artifacts {
        match fs::read(a) {
            Ok(b) => artifact_blobs.push(b),
            Err(_) => println!("COURIER-WARN cannot read artifact {}", a.display()),
        }
    }
    let has_token = |tok: &str| -> Option<bool> {
        if tok.is_empty() {
            return None;
        }
        if artifact_blobs.is_empty() {
            return None;
        }
        let t = tok.as_bytes();
        Some(
            artifact_blobs
                .iter()
                .any(|b| b.windows(t.len()).any(|w| w == t)),
        )
    };
    println!("{:<48} {:>8}  {}", "transport", "packets", "instrument");
    let mut violations: Vec<String> = Vec::new();
    for row in TRANSPORTS {
        let mut n = 0u64;
        for pat in row.patterns {
            let re = Regex::new(pat).expect("transport pattern is a literal of this file");
            n += text.lines().filter(|l| re.is_match(l)).count() as u64;
        }
        let (count, inst) = match row.instrument {
            Some(i) => match has_token(row.proof_token) {
                Some(false) => (
                    "UNMEASURED".to_string(),
                    "instrument ABSENT from the deployed artifact",
                ),
                _ => (format!("{n}"), i),
            },
            None => ("UNMEASURED".to_string(), "no instrument in the tree yet"),
        };
        // OPT-IN INSTRUMENT (directive section 12): a zero from an env-gated trace measures the TRACE FLAG, not the
        // traffic. Report it as UNMEASURED, because a fabricated zero row is worse than an admitted gap.
        let (count, inst) = if row.opt_in_instrument && count == "0" && !inst.contains("ABSENT") {
            (
                "UNMEASURED".to_string(),
                format!("{inst} -- trace not enabled in this run"),
            )
        } else {
            (count, inst.to_string())
        };
        println!("{:<48} {:>8}  {}", row.name, count, inst);
        if row.must_be_zero {
            if inst.starts_with("instrument ABSENT") || inst.starts_with("no instrument") {
                violations.push(format!("{} UNMEASURED ({inst})", row.name));
            } else if n > 0 {
                violations.push(format!("{} = {n} (must be 0)", row.name));
            }
        }
    }
    // KIND-LEVEL DELIVERY ACCOUNTING (user directive: the question "who received the bundle" must not require
    // reading the server's source). The server already logs every courier bundle with its kind and whether the
    // target connection is the LOADER's (`[courier-send-image] bundle-to pid=.. kind=.. isLoader=..`). Aggregating
    // that answers, in one command, a question that cost a source dive: which kinds reach the loader and which
    // reach only the guest image. The generic verdict flags any kind delivered ONLY to isLoader=0, because for the
    // process doorbell that is exactly the defect the wake census exposed (36 of 38 publishes had no doorbell).
    {
        use std::collections::BTreeMap;
        let mut by_kind: BTreeMap<(String, String), u64> = BTreeMap::new();
        for line in text.lines() {
            let Some(pos) = line.find("bundle-to ") else {
                continue;
            };
            let seg = &line[pos..];
            let field = |name: &str| -> Option<String> {
                let at = seg.find(name)?;
                let rest = &seg[at + name.len()..];
                let end = rest.find(|c: char| c.is_whitespace()).unwrap_or(rest.len());
                Some(rest[..end].to_string())
            };
            if let (Some(kind), Some(loader)) = (field("kind="), field("isLoader=")) {
                *by_kind.entry((kind, loader)).or_insert(0) += 1;
            }
        }
        if by_kind.is_empty() {
            println!(
                "COURIER-KINDS none -- the server's per-bundle delivery record is env-gated; re-run the workload with DARLING_SERVER_COURIER_LOG=1 to account for delivery by kind"
            );
        }
        if !by_kind.is_empty() {
            println!("COURIER-KINDS kind loader count");
            for ((kind, loader), n) in &by_kind {
                println!("  {kind:<22} isLoader={loader}  {n}");
            }
            let kinds: Vec<&String> = by_kind.keys().map(|(k, _)| k).collect();
            let mut only_image: Vec<&String> = Vec::new();
            for k in kinds {
                let to_loader: u64 = by_kind
                    .iter()
                    .filter(|((kk, l), _)| kk == k && l == "1")
                    .map(|(_, n)| *n)
                    .sum();
                let to_image: u64 = by_kind
                    .iter()
                    .filter(|((kk, l), _)| kk == k && l == "0")
                    .map(|(_, n)| *n)
                    .sum();
                if to_loader == 0 && to_image > 0 {
                    only_image.push(k);
                }
            }
            if only_image.is_empty() {
                println!(
                    "COURIER-KINDS-VERDICT every delivered kind reached the loader connection at least once"
                );
            } else {
                println!(
                    "COURIER-KINDS-VERDICT kind(s) delivered ONLY to the guest image (isLoader=0): {}",
                    only_image
                        .iter()
                        .map(|k| k.as_str())
                        .collect::<Vec<_>>()
                        .join(", ")
                );
            }
        }
    }
    if violations.is_empty() {
        println!("COURIER-VERDICT PASS legacy-ordinary-afunix=0 zero-fd-control=0");
        Ok(ExitCode::SUCCESS)
    } else {
        for v in &violations {
            println!("COURIER-VIOLATION {v}");
        }
        println!("COURIER-VERDICT FAIL violations={}", violations.len());
        Ok(ExitCode::from(1))
    }
}

#[cfg(test)]
mod witness_tests {
    use super::*;

    // ONE SAMPLE LINE PER INSTRUMENT, and the assertion that the registered pattern matches it. Why this exists: the
    // registry is what makes the census a census, and it rots the moment an instrument's FORMAT changes -- which
    // happened for real, twice in one session: `SEM-SITE` gained a `tcb=` field so `^SEM-SITE op=` stopped matching,
    // and the ring dump was rewritten from `trace-record` to `dtape.ering seq=`. In both cases the instrument was on
    // and the census called it silent. A format change now fails here instead of silently emptying a report.
    const SAMPLES: &[(&str, &str)] = &[
        (
            "sem-site",
            "SEM-SITE tcb=0x7d3d03e9e8c0 op=timedwait a=0x903 b=0x1E ra=0x71DA7C883893 sym0=semaphore_timedwait_trap_impl",
        ),
        ("iter-marks", "ITER 1 tid=1282542 port=2307 create"),
        (
            "stall-dump",
            "[1790512650.010866](stall-dump, Error) stall-dump idle_ms=5136 serviced=571 processes=4 threads=8",
        ),
        (
            "ring-dump",
            "dtape.ering seq=453 tag=timer_arm a=0x62f094848e213 b=0x0 c=0x62f024c254e7f d=0x0",
        ),
        (
            "plane-refuse",
            "[plane-refuse] why=no-slot op=24 a=1 b=103079215108 tid=1274647",
        ),
        (
            "dtape-msgq",
            "dtape.msgq event=post_wake thread=0x653875af3de8 mqueue=0x653875aef3e8 arg=0x0 bits=0x300000000 result=1",
        ),
        (
            "dtape-timer",
            "dtape.wait_timer event=thread_unblock thread=0x60604ca39ee8 wait_result=0 had_timer=0 active=0",
        ),
        (
            "rpc-begin",
            "rpc.semaphore.begin operation=semaphore_timedwait pid=1 tid=1250752 wait_name=2307 signal_name=none sec=30 nsec=0",
        ),
        (
            "rpc-reply",
            "rpc.semaphore.reply operation=semaphore_timedwait pid=1 tid=1250752 wait_name=2307 signal_name=none code=49 outcome=timeout terminal=reply-enqueued",
        ),
        (
            "crash",
            "[dserver-CRASH sig=b addr=0x0,self=61d5aa4f9060,ret=0x0,sp=0x7de2c8112bf0,pc=0x61d5aa61229b",
        ),
        ("workload-stall", "RING_MACH_TEST_STALL age=31.9"),
        ("execpath-after", "seq=4 after-execpath status=0"),
        (
            "sigexc",
            "[sigexc-fatal sig=11 code=1 addr=0x0 pid=2678054 tid=2678054]",
        ),
        (
            "plane-slow",
            "[plane-slow op=24 state=1 rseq=7 rs=-1 rq=0 tid=1274647]",
        ),
        (
            "modrefs",
            "[modrefs-entry target=1 name=2 right=3 delta=-1]",
        ),
        ("allocprobe", "[allocprobe]"),
        (
            "ring-trace-gen",
            "RING_TRACE gen ENTER callnum=9 tid=1250752",
        ),
        ("iter-drop", "ITER 3 tid=1282542 drop_enter"),
        (
            "rpc-socket-denied",
            "[rpc-socket-DENIED] pid=1 tid=2 call=pthread_kill image=kernel delta=0x1a denied=1",
        ),
        (
            "checkout-path",
            "[checkout-path] pid=1 tid=2 page=0x7f ready=0 main=1",
        ),
        (
            "checkin-path",
            "[checkin-path] pid=1 tid=2 page=0x7f ready=0 -> no-transport (declined)",
        ),
        (
            "release-drops-pending",
            "[release-drops-pending] site=dserver-ring.c:2752",
        ),
    ];

    #[test]
    fn every_registered_instrument_matches_its_sample() {
        for (name, sample) in SAMPLES {
            let (_, pattern, _) = INSTRUMENTS
                .iter()
                .find(|(n, _, _)| n == name)
                .unwrap_or_else(|| panic!("instrument {name} is registered nowhere"));
            let re = Regex::new(pattern).unwrap();
            assert!(
                re.is_match(sample),
                "instrument {name} does not match its own sample line"
            );
        }
    }

    #[test]
    fn every_instrument_has_a_sample() {
        for (name, _, _) in INSTRUMENTS {
            assert!(
                SAMPLES.iter().any(|(n, _)| n == name),
                "instrument {name} has no sample line, so its pattern is unchecked"
            );
        }
    }
}
