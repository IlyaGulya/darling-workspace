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
use regex::Regex;
use clap::Args;
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
        bail!("llvm-nm failed for {}: {}", binary.display(), String::from_utf8_lossy(&out.stderr));
    }
    let text = String::from_utf8_lossy(&out.stdout);
    let mut syms = Vec::new();
    for line in text.lines() {
        let mut parts = line.split_whitespace();
        let (Some(addr), Some(_kind), Some(name)) = (parts.next(), parts.next(), parts.next()) else {
            continue;
        };
        if let Ok(addr) = u64::from_str_radix(addr, 16) {
            // A universal binary lists every name once per slice; the first occurrence is the x86_64 slice that the
            // deltas in this workspace are measured against, and the caller is told which base answered.
            if syms.iter().any(|s: &Symbol| s.name == name) {
                continue;
            }
            syms.push(Symbol { addr, name: name.to_string() });
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
            near.iter().map(|n| format!("\"{n}\"")).collect::<Vec<_>>().join(",")
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
    #[arg(long)]
    binary: PathBuf,
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
    let mut c = CrashLine { raw: line.trim().to_string(), ..Default::default() };
    // EVERY marker is parsed out of EVERY comma-field. MEASURED: a first-match-else chain left `addr` empty, because
    // `sig=` and `addr=` appear in the SAME field of a real line (`[dserver-CRASH sig=b addr=0x0`) -- the parse looked
    // right, produced a parseable document, and silently dropped a field. Independent extraction is the fix.
    for field in line.split(',') {
        let field = field.trim();
        if let Some(i) = field.find("sig=") {
            c.sig = field[i + 4..].split_whitespace().next().unwrap_or("").to_string();
        }
        if let Some(i) = field.find("addr=") {
            c.addr = field[i + 5..].split_whitespace().next().unwrap_or("").to_string();
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

fn run_crash(args: CrashArgs) -> Result<ExitCode> {
    let line = match (&args.line, &args.log) {
        (Some(l), _) => l.clone(),
        (None, Some(log)) => {
            let text = fs::read_to_string(log).with_context(|| format!("reading {}", log.display()))?;
            text.lines()
                .find(|l| l.contains("dserver-CRASH"))
                .map(|l| l.to_string())
                .with_context(|| format!("no dserver-CRASH line in {}", log.display()))?
        }
        _ => bail!("need --line or --log"),
    };
    let crash = parse_crash_line(&line);
    let syms = load_symbols(&args.binary)?;

    let resolve = |addr: u64| -> Option<String> {
        locate(&syms, addr).map(|(i, off)| format!("{} + 0x{:x}", symbol_name(&syms[i].name), off))
    };

    // The probe reports `self` (a known symbol's runtime address) precisely so that the runtime pc can be turned into an
    // offset inside the FILE; without that subtraction the number is only meaningful to the process that printed it.
    let file_target = match (crash.self_, crash.pc) {
        (Some(s), Some(pc)) => {
            let self_file = syms
                .iter()
                .find(|s| symbol_name(&s.name).contains("dserver_crash_probe"))
                .map(|s| s.addr)
                .context("dserver_crash_probe not in the symbol table: cannot derive the file offset")?;
            Some(self_file + (pc.saturating_sub(s)))
        }
        _ => crash.pc,
    };

    let location = file_target.and_then(|t| locate(&syms, t).map(|(i, o)| (t, i, o)));
    let mut stack_locs: Vec<(usize, u64, String)> = Vec::new();
    if let Some(self_runtime) = crash.self_ {
        for (i, w) in crash.stack.iter().enumerate() {
            if *w == 0 {
                continue;
            }
            let delta = w.saturating_sub(self_runtime);
            if delta < 0x80_0000 {
                if let Some((si, off)) = locate(&syms, syms.iter().find(|s| symbol_name(&s.name).contains("dserver_crash_probe")).map(|s| s.addr).unwrap_or(0) + delta) {
                    stack_locs.push((i, *w, format!("{} + 0x{:x}", symbol_name(&syms[si].name), off)));
                }
            }
        }
    }

    let mut disassembly = String::new();
    if let (Some(t), Some(ctx)) = (file_target, Some(args.context)) {
        let lo = t.saturating_sub(ctx);
        let hi = t + ctx;
        let out = Command::new("objdump")
            .args(["-d", &format!("--start-address=0x{lo:x}"), &format!("--stop-address=0x{hi:x}")])
            .arg(&args.binary)
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

    if args.json {
        let escaped = jesc(&disassembly);
        println!(
            "{{\"crash\":\"{}\",\"sig\":\"{}\",\"addr\":\"{}\",\"location\":\"{}\",\"stack\":[{}],\"disassembly\":\"{}\"}}",
            jesc(&crash.raw),
            jesc(&crash.sig),
            jesc(&crash.addr),
            location.as_ref().map(|(_, i, o)| format!("{} + 0x{:x}", symbol_name(&syms[*i].name), o)).unwrap_or_default(),
            stack_locs
                .iter()
                .map(|(i, _, s)| format!("{{\"w\":{},\"location\":\"{}\"}}", i, s))
                .collect::<Vec<_>>()
                .join(","),
            escaped
        );
    } else {
        println!("crash: {}", crash.raw);
        if let Some((t, i, o)) = &location {
            println!("target=0x{t:x}");
            println!("LOCATION: {} + 0x{o:x}", symbol_name(&syms[*i].name));
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
    #[arg(long)]
    prefix: PathBuf,
    #[arg(long)]
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
}

impl Verdict {
    fn ok(&self) -> bool {
        self.verdict == "PASS"
    }
}

fn run_one_workload(args: &VerdictArgs) -> Result<Verdict> {
    let log = std::env::temp_dir().join(format!("dwdiag-verdict-{}-{}.log", std::process::id(), args.mode));
    let _ = fs::remove_file(&log);
    let cmd = format!("{} {} {}", args.guest_command, args.mode, args.args);
    let mut c = Command::new(&args.boot_runner);
    c.arg("--prefix")
        .arg(&args.prefix)
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
    // A marker that can never appear: the harness exits non-zero, and the VERDICT below is ours, not its marker test.
    c.arg("--marker").arg("__dwdiag_never__");
    let out = c.output().with_context(|| format!("running {}", args.boot_runner.display()))?;

    // The harness's own exit code is deliberately ignored: it reports whether its MARKERS appeared, which is not the
    // question here (MEASURED: a marker matching the workload's start line made a hang look like a pass).
    let _ = out;

    let text = fs::read_to_string(&log).unwrap_or_default();
    let result_prefix = format!("RING_MACH_TEST mode={} ", args.mode);
    let line = text
        .lines()
        .find(|l| l.contains(&result_prefix))
        .map(|l| l.trim().to_string())
        .unwrap_or_default();
    let denied = text.lines().filter(|l| l.contains("rpc-socket-DENIED")).count() as u64;
    let created = text.lines().filter(|l| l.contains("rpc-socket] created") || l.contains("rpc-socket. created")).count() as u64;
    let started = text.contains(&format!("mode={}", args.mode));

    let verdict = if line.is_empty() {
        if started { "HANG" } else { "NO-RUN" }.to_string()
    } else if line.contains("pass=1") {
        "PASS".to_string()
    } else {
        "FAIL".to_string()
    };

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
                        if let Some(base) = s.iter().find(|s| symbol_name(&s.name) == "mach_driver_get_fd") {
                            if let Ok(off) = parse_hex(&d) {
                                if let Some((i, o)) = locate(&s, base.addr + off) {
                                    denial_location = Some(format!("{} + 0x{:x}", symbol_name(&s[i].name), o));
                                }
                            }
                        }
                    }
                }
            }
        }
    }

    Ok(Verdict { mode: args.mode.clone(), verdict, denied, created, line, denial_call, denial_location, log })
}

fn run_verdict(args: VerdictArgs) -> Result<ExitCode> {
    let v = run_one_workload(&args)?;
    if args.json {
        println!(
            "{{\"mode\":\"{}\",\"verdict\":\"{}\",\"denied\":{},\"created\":{},\"result\":\"{}\",\"denial_call\":{},\"denial_location\":{},\"log\":\"{}\"}}",
            v.mode,
            v.verdict,
            v.denied,
            v.created,
            jesc(&v.line),
            v.denial_call.as_ref().map(|c| format!("\"{c}\"")).unwrap_or_else(|| "null".into()),
            v.denial_location.as_ref().map(|c| format!("\"{c}\"")).unwrap_or_else(|| "null".into()),
            jesc(&v.log.display().to_string())
        );
    } else {
        let extra = match (&v.denial_call, &v.denial_location) {
            (Some(c), Some(l)) => format!(" first-denial={c} caller={l}"),
            (Some(c), None) => format!(" first-denial={c}"),
            _ => String::new(),
        };
        println!(
            "VERDICT mode={} verdict={} denied={} created={}{} :: {}",
            v.mode,
            v.verdict,
            v.denied,
            v.created,
            extra,
            if v.line.is_empty() { "<no result line>" } else { &v.line }
        );
        // COMPOSED, not repeated: a non-PASS verdict is useless without the stage, and reading it out of two logs by
        // hand is exactly the work this tool exists to remove (doc section 230 -- the stall was found by grepping
        // `[mldr-ctl]` and `process-control-service` by hand, three runs in a row).
        if v.verdict != "PASS" {
            let text = fs::read_to_string(&v.log).unwrap_or_default();
            let guest_log = std::env::var("MLDR_DIAG_LOG").ok().map(PathBuf::from);
            let guest = guest_log.as_ref().and_then(|p| fs::read_to_string(p).ok()).unwrap_or_default();
            let p = summarize_progress(&text, &guest, &v.mode);
            println!(
                "VERDICT-STAGE workload={} last-guest={} last-published-op={} last-served-op={} serviced={}",
                p.workload,
                if p.last_guest.is_empty() { "<none>" } else { &p.last_guest },
                if p.last_published_op.is_empty() { "<none>" } else { &p.last_published_op },
                if p.last_served_op.is_empty() { "<none>" } else { &p.last_served_op },
                p.serviced
            );
        }
    }
    Ok(if v.ok() { ExitCode::SUCCESS } else { ExitCode::from(1) })
}

#[derive(Args, Debug)]
pub struct SuiteArgs {
    #[arg(long)]
    prefix: PathBuf,
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
            prefix: args.prefix.clone(),
            mode: mode.clone(),
            args: margs.clone(),
            wait: args.wait_base + extra,
            env: args.env.clone(),
            boot_runner: args.boot_runner.clone(),
            guest_command: "/usr/bin/ring_mach_msg_test".to_string(),
            guest_symbols: args.guest_symbols.clone(),
            json: false,
        };
        let v = run_one_workload(&va)?;
        if !v.ok() {
            failures += 1;
        }
        if args.require_zero_creations && v.created != 0 {
            failures += 1;
        }
        if !args.json {
            println!(
                "{:<28} {:<9} {:<8} {:<8} {}",
                format!("{} {}", v.mode, margs).trim(),
                v.verdict,
                v.denied,
                v.created,
                if v.line.is_empty() { "<no result line>".to_string() } else { v.line.clone() }
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
        println!("SUITE rows={} failures={} require_zero_creations={}", rows.len(), failures, args.require_zero_creations as u8);
        println!("SUITE-VERDICT {}", if pass { "PASS" } else { "FAIL" });
    }
    Ok(if pass { ExitCode::SUCCESS } else { ExitCode::from(1) })
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
    /// The workload's mode, used to look for its own machine-readable result line.
    #[arg(long, default_value = "")]
    mode: String,
    #[arg(long)]
    json: bool,
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

fn summarize_progress(run: &str, guest: &str, mode: &str) -> Progress {
    let mut p = Progress::default();
    let want = if mode.is_empty() { "RING_MACH_TEST mode=".to_string() } else { format!("RING_MACH_TEST mode={mode} ") };
    if let Some(l) = run.lines().find(|l| l.contains(&want)) {
        p.workload = "present".to_string();
        p.result = l.trim().to_string();
    } else if run.contains("RING_MACH_TEST mode=") {
        p.workload = "other-mode".to_string();
    } else {
        p.workload = "absent".to_string();
    }
    p.denied = run.lines().filter(|l| l.contains("rpc-socket-DENIED")).count() as u64;
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
    for l in run.lines().filter(|l| l.contains("process-control-service")) {
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
            "iter" => (0, format!("spin {}", rest.split_whitespace().take(3).collect::<Vec<_>>().join(" "))),
            "plane-request" if rest.contains("TIMEOUT") => (9, format!("TIMEOUT {}", rest)),
            "seq" => {
                // `seq=N after-<stage> pid=... image=...` -- the bootstrap stage names, which is what a stall is read
                // against when the workload never speaks.
                let stage = rest.split_whitespace().find(|w| w.starts_with("after-") || w.starts_with("before-"));
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
    ("sem-site", r"^SEM-SITE op=", "guest: who calls the semaphore family, and with which name/address"),
    ("iter-marks", r"^ITER [0-9]+ ", "guest workload: per-iteration progress, names the iteration that stopped"),
    ("stall-dump", r"stall-dump idle_ms=", "server: parked threads, their calls, and their wait-timer state"),
    ("ring-dump", r"dtape\.ering (dump|seq=)", "server: the in-memory event ring dumped when the counters stop"),
    ("plane-refuse", r"plane-refuse", "server: a plane request refused at the op that refused it"),
    ("dtape-msgq", r"dtape\.msgq event=", "server: msgq park/send/post/wake order"),
    ("dtape-timer", r"dtape\.wait_timer event=", "server: wait-timer prepare/expire/unblock"),
    ("rpc-begin", r"rpc\.[a-z_0-9]+\.begin", "server: an RPC request the server began"),
    ("rpc-reply", r"rpc\.[a-z_0-9]+\.reply", "server: an RPC reply the server enqueued (begin without reply is a stall)"),
    ("crash", r"dserver-CRASH", "server: the crash probe, with its fault address and stack walk"),
    ("workload-stall", r"RING_MACH_TEST_STALL", "guest workload: its own watchdog fired"),
    ("execpath-after", r"after-execpath", "server: the post-exec completion-store barrier"),
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
        None => newest_verdict_log().context("no `dwdiag-verdict-*.log` in the temp directory; pass --log")?,
    };
    let text = fs::read_to_string(&log).with_context(|| format!("reading {}", log.display()))?;
    let census = witness_census(&text);
    let fired: Vec<_> = census.iter().filter(|(_, c, _)| *c > 0).collect();
    let silent: Vec<_> = census.iter().filter(|(_, c, _)| *c == 0).map(|(n, _, _)| n.clone()).collect();
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
        println!("WITNESS log={} instruments={} fired={}", log.display(), census.len(), fired.len());
        for (name, count, sample) in &census {
            if *count > 0 {
                println!("  {name:<14} {count:>7}  {sample}");
            }
        }
        println!(
            "WITNESS-SILENT {}",
            if silent.is_empty() { "<none>".to_string() } else { silent.join(",") }
        );
    }
    Ok(ExitCode::SUCCESS)
}

fn run_progress(args: ProgressArgs) -> Result<ExitCode> {
    let run = fs::read_to_string(&args.log).unwrap_or_default();
    let guest = args.guest_log.as_ref().map(|p| fs::read_to_string(p).unwrap_or_default()).unwrap_or_default();
    let p = summarize_progress(&run, &guest, &args.mode);
    if args.json {
        println!(
            "{{\"workload\":\"{}\",\"result\":\"{}\",\"last_guest\":\"{}\",\"last_published_op\":\"{}\",\"last_served_op\":\"{}\",\"serviced\":{},\"denied\":{},\"created\":{},\"first_denial_call\":\"{}\"}}",
            jesc(&p.workload), jesc(&p.result), jesc(&p.last_guest), jesc(&p.last_published_op),
            jesc(&p.last_served_op), p.serviced, p.denied, p.created, jesc(&p.first_denial_call)
        );
    } else {
        println!(
            "PROGRESS workload={} last-guest={} last-published-op={} last-served-op={} serviced={} denied={} created={} first-denial={}",
            p.workload,
            if p.last_guest.is_empty() { "<none>" } else { &p.last_guest },
            if p.last_published_op.is_empty() { "<none>" } else { &p.last_published_op },
            if p.last_served_op.is_empty() { "<none>" } else { &p.last_served_op },
            p.serviced, p.denied, p.created,
            if p.first_denial_call.is_empty() { "<none>" } else { &p.first_denial_call }
        );
        if !p.result.is_empty() {
            println!("PROGRESS-RESULT {}", p.result);
        }
    }
    // The exit code is a QUESTION ("did the workload speak?"), not a judgement: absent means the caller must look at
    // `last-guest`/`last-served-op`, present means the run got far enough to be judged on the result line itself.
    Ok(if p.workload == "present" { ExitCode::SUCCESS } else { ExitCode::from(1) })
}

// ----------------------------------------------------------------------------------------------------------------

pub fn dispatch(cmd: DiagCommand) -> Result<ExitCode> {
    match cmd {
        DiagCommand::Symbolize(a) => run_symbolize(a),
        DiagCommand::Crash(a) => run_crash(a),
        DiagCommand::Verdict(a) => run_verdict(a),
        DiagCommand::Suite(a) => run_suite(a),
        DiagCommand::Progress(a) => run_progress(a),
        DiagCommand::Witness(a) => run_witness(a),
    }
}

#[derive(clap::Subcommand, Debug)]
pub enum DiagCommand {
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
    /// Report which INSTRUMENTS fired in a run log, and which stayed SILENT. A registered instrument with zero hits is
    /// the failure this exists to catch: a guard that silently does nothing cannot be told from no guard at all, and a
    /// hand-written grep over one log finds a line but cannot tell you which of the other instruments never spoke.
    Witness(WitnessArgs),
}

// A tiny helper used by the table so a caller can see the set in a stable order under --json too.
#[allow(dead_code)]
fn btree_from(pairs: Vec<(String, String)>) -> BTreeMap<String, String> {
    pairs.into_iter().collect()
}
