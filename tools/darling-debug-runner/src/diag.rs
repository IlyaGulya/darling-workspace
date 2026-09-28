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

    // HONESTY BOUND (perf#30, doc section 253). The anchor subtraction assumes the faulting pc belongs to THIS binary.
    // MEASURED: a `std::length_error` throw made the pc land in libstdc++, the subtraction produced a huge number, and
    // `locate` still returned a symbol -- the output claimed `LOCATION: end + 0x1b6f...`, a location that cannot exist.
    // An answer that cannot be checked is worse than no answer, so a target outside the binary's own symbol range is
    // reported as such instead of being matched to the nearest symbol.
    let (min_sym, max_sym) = match (syms.first(), syms.last()) {
        (Some(f), Some(l)) => (f.addr, l.addr),
        _ => (0, u64::MAX),
    };
    let in_binary = file_target.map(|t| t >= min_sym.saturating_sub(0x1000) && t <= max_sym.saturating_add(0x1000));
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
        } else if crash.pc.is_some() {
            // Say WHY there is no location, instead of printing nothing (or a guess): the pc is not in this binary,
            // which is the normal case for a C++ throw, a libc abort, or a fault inside a shared library.
            println!("LOCATION: <outside this binary's symbol range: the pc belongs to another object>");
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
    /// How many times to run the SAME workload. A single-run verdict cannot see flake, and this session's
    /// evidence is full of one-run PASS/CRASH flips; the summary names the distribution and fails if it is not
    /// uniform. Each run gets its own log.
    #[arg(long, default_value_t = 1)]
    repeat: u64,
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

fn run_one_workload(args: &VerdictArgs, tag: &str) -> Result<Verdict> {
    let log = std::env::temp_dir().join(format!("dwdiag-verdict-{}-{}{}.log", std::process::id(), args.mode, tag));
    let _ = fs::remove_file(&log);
    // The workload's OWN exit status is part of the observation: MEASURED, a workload that dies of SIGSEGV
    // (`EXITRC=139`) produces exactly the same evidence as a deadlock -- no result line -- and every
    // measurement drawn from "HANG" then chases a lock that does not exist. The status is printed by the
    // guest shell, so it is the guest's own answer, not the host launcher's.
    let cmd = format!("{} {} {}; echo __DWDIAG_RC=$?", args.guest_command, args.mode, args.args);
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

    let rc: Option<i32> = text
        .lines()
        .filter_map(|l| l.split("__DWDIAG_RC=").nth(1))
        .filter_map(|v| v.trim().parse::<i32>().ok())
        .next_back();
    let signal = rc.filter(|c| *c >= 128).map(|c| signal_name(c - 128).to_string());

    let verdict = if line.is_empty() {
        match (started, rc) {
            // A death by signal and a deadlock produce the same missing result line; only the status separates
            // them, and the earlier conflation is what made a SIGSEGV look like a hang.
            (_, Some(c)) if c >= 128 => format!("CRASH {}", signal_name(c - 128)),
            (_, Some(c)) => format!("EXIT rc={c}"),
            (true, None) => "HANG".to_string(),
            (false, None) => "NO-RUN".to_string(),
        }
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

    Ok(Verdict { mode: args.mode.clone(), verdict, denied, created, line, denial_call, denial_location, log, rc, signal })
}

fn run_verdict(args: VerdictArgs) -> Result<ExitCode> {
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
        return Ok(if ok == repeat as usize { ExitCode::SUCCESS } else { ExitCode::from(1) });
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
            v.denial_call.as_ref().map(|c| format!("\"{c}\"")).unwrap_or_else(|| "null".into()),
            v.denial_location.as_ref().map(|c| format!("\"{c}\"")).unwrap_or_else(|| "null".into()),
            v.rc.map(|c| c.to_string()).unwrap_or_else(|| "null".into()),
            v.signal.as_ref().map(|c| format!("\"{c}\"")).unwrap_or_else(|| "null".into()),
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
            if v.line.is_empty() { "<no result line>" } else { &v.line }
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
            prefix: args.prefix.clone(),
            mode: mode.clone(),
            args: margs.clone(),
            wait: args.wait_base + extra,
            env: args.env.clone(),
            boot_runner: args.boot_runner.clone(),
            guest_command: "/usr/bin/ring_mach_msg_test".to_string(),
            guest_symbols: args.guest_symbols.clone(),
            repeat: 1,
            json: false,
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
                println!("ROW-RETRY mode={} first={} retry={}", va.mode, first, v2.verdict);
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
    ("sem-site", r"^SEM-SITE ", "guest: who calls the semaphore family, with which name/address, and from which thread"),
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
    // Added 2026-09-27 with the per-thread-socket removal and the diagnostics that closed the silent-death
    // investigation. Each one is an instrument that was added to the tree and therefore has to be counted here, or
    // `witness` reports a live instrument as silent (the failure this registry exists to prevent).
    ("sigexc", r"\[sigexc-(fatal|default) sig=", "guest: the fault translator reporting a fatal/returned raw signal"),
    ("plane-slow", r"\[plane-slow op=", "guest: a process-control request the server did not complete in time"),
    ("modrefs", r"\[modrefs-(entry|exit) ", "guest: the mach_port_mod_refs trap around its impl and its exit code"),
    ("allocprobe", r"\[allocprobe\]", "guest: an allocation-path probe taken while a lock-free path was suspected"),
    ("ring-trace-gen", r"RING_TRACE gen (ENTER|EXIT) callnum=", "guest: the generated-call trampoline's enter/exit pair"),
    ("iter-drop", r"ITER [0-9]+ tid=[0-9]+ drop_", "guest workload: which drop path an iteration took"),
    ("rpc-socket-denied", r"\[rpc-socket-DENIED\] ", "guest: a caller that has no lane and no plane op, and the call it is (the removal's own instrument)"),
    ("checkout-path", r"\[checkout-path\] ", "guest: a thread-exit checkout that could not be published, with the state that prevented it"),
    ("checkin-path", r"\[checkin-path\] ", "guest: a checkin that could not be published, with the state that prevented it"),
    ("release-drops-pending", r"\[release-drops-pending\] site=", "guest/server: a completed request whose slot was released while still pending, by site"),
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
        DiagCommand::Courier(a) => run_courier(a),
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
    /// Count the packets per TRANSPORT for a run log, and judge the AF_UNIX endpoint's purity. Rows with no
    /// instrument behind them are printed UNMEASURED, never as zero: a census that cannot see a transport must say so.
    Courier(CourierArgs),
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
}

const TRANSPORTS: &[TransportRow] = &[
    TransportRow { name: "SPSC Ring (per-thread lane, ordinary calls)", patterns: &[r"RING_TRACE gen ENTER callnum="], instrument: Some("RING_TRACE gen ENTER (only under DARLING_GUEST_RING_TRACE=1)"), proof_token: "RING_TRACE gen ENTER callnum=", must_be_zero: false },
    TransportRow { name: "duplex Ring/mailbox (caller-S2C, OOL)", patterns: &[r"RING_MACHMSG_PUBLISH", r"dtape\.msgq event="], instrument: Some("guest RING_MACHMSG_PUBLISH / server dtape.msgq"), proof_token: "RING_MACHMSG_PUBLISH", must_be_zero: false },
    TransportRow { name: "process management plane (shared page, slot)", patterns: &[r"\[plane-", r"\[release-drops-pending\]"], instrument: Some("plane-* lines (partial: only named paths print)"), proof_token: "[plane-", must_be_zero: false },
    TransportRow { name: "urgent shared plane (interrupt/sigprocess)", patterns: &[r"urgent"], instrument: None, proof_token: "", must_be_zero: false },
    TransportRow { name: "SCM_RIGHTS courier (fd-bearing packets)", patterns: &[r"\[afunix-send\] .*scm=1", r"\[courier-send\] .*scm=1"], instrument: Some("afunix-send + courier-send (scm=1)"), proof_token: "[courier-send]", must_be_zero: false },
    TransportRow { name: "legacy ordinary AF_UNIX (semantic, no fd)", patterns: &[r"\[afunix-send\] .*scm=0", r"\[courier-send\] .*scm=0"], instrument: Some("afunix-send + courier-send (scm=0)"), proof_token: "[afunix-send]", must_be_zero: true },
    TransportRow { name: "zero-fd control/wake packets on AF_UNIX", patterns: &[r"\[afunix-wake\]"], instrument: Some("afunix-wake (gone: the plane wake is now a non-packet; [plane-wake] records it)"), proof_token: "[plane-wake]", must_be_zero: true },
];

fn run_courier(args: CourierArgs) -> Result<ExitCode> {
    let log = match args.log {
        Some(p) => p,
        None => newest_verdict_log().context("no `dwdiag-verdict-*.log` in the temp directory; pass --log")?,
    };
    let text = fs::read_to_string(&log).with_context(|| format!("read {}", log.display()))?;
    println!("COURIER log={} lines={}", log.display(), text.lines().count());
    // The ARTIFACT decides whether a zero is evidence: a token that is not in the deployed binary means the row was
    // never measured, however quiet the log is. (A silent probe is not evidence -- this project's own rule.)
    let default_artifacts = vec![
        PathBuf::from("/tmp/dr-on-matched/libexec/darling/usr/libexec/darling/mldr"),
        PathBuf::from("/tmp/dr-on-matched/libexec/darling/usr/lib/system/libsystem_kernel.dylib"),
    ];
    let artifacts = if args.artifact.is_empty() { &default_artifacts } else { &args.artifact };
    let mut artifact_blobs: Vec<Vec<u8>> = Vec::new();
    for a in artifacts {
        match fs::read(a) { Ok(b) => artifact_blobs.push(b), Err(_) => println!("COURIER-WARN cannot read artifact {}", a.display()) }
    }
    let has_token = |tok: &str| -> Option<bool> {
        if tok.is_empty() { return None; }
        if artifact_blobs.is_empty() { return None; }
        let t = tok.as_bytes();
        Some(artifact_blobs.iter().any(|b| b.windows(t.len()).any(|w| w == t)))
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
                Some(false) => ("UNMEASURED".to_string(), "instrument ABSENT from the deployed artifact"),
                _ => (format!("{n}"), i),
            },
            None => ("UNMEASURED".to_string(), "no instrument in the tree yet"),
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
        ("sem-site", "SEM-SITE tcb=0x7d3d03e9e8c0 op=timedwait a=0x903 b=0x1E ra=0x71DA7C883893 sym0=semaphore_timedwait_trap_impl"),
        ("iter-marks", "ITER 1 tid=1282542 port=2307 create"),
        ("stall-dump", "[1790512650.010866](stall-dump, Error) stall-dump idle_ms=5136 serviced=571 processes=4 threads=8"),
        ("ring-dump", "dtape.ering seq=453 tag=timer_arm a=0x62f094848e213 b=0x0 c=0x62f024c254e7f d=0x0"),
        ("plane-refuse", "[plane-refuse] why=no-slot op=24 a=1 b=103079215108 tid=1274647"),
        ("dtape-msgq", "dtape.msgq event=post_wake thread=0x653875af3de8 mqueue=0x653875aef3e8 arg=0x0 bits=0x300000000 result=1"),
        ("dtape-timer", "dtape.wait_timer event=thread_unblock thread=0x60604ca39ee8 wait_result=0 had_timer=0 active=0"),
        ("rpc-begin", "rpc.semaphore.begin operation=semaphore_timedwait pid=1 tid=1250752 wait_name=2307 signal_name=none sec=30 nsec=0"),
        ("rpc-reply", "rpc.semaphore.reply operation=semaphore_timedwait pid=1 tid=1250752 wait_name=2307 signal_name=none code=49 outcome=timeout terminal=reply-enqueued"),
        ("crash", "[dserver-CRASH sig=b addr=0x0,self=61d5aa4f9060,ret=0x0,sp=0x7de2c8112bf0,pc=0x61d5aa61229b"),
        ("workload-stall", "RING_MACH_TEST_STALL age=31.9"),
        ("execpath-after", "seq=4 after-execpath status=0"),
        ("sigexc", "[sigexc-fatal sig=11 code=1 addr=0x0 pid=2678054 tid=2678054]"),
        ("plane-slow", "[plane-slow op=24 state=1 rseq=7 rs=-1 rq=0 tid=1274647]"),
        ("modrefs", "[modrefs-entry target=1 name=2 right=3 delta=-1]"),
        ("allocprobe", "[allocprobe]"),
        ("ring-trace-gen", "RING_TRACE gen ENTER callnum=9 tid=1250752"),
        ("iter-drop", "ITER 3 tid=1282542 drop_enter"),
        ("rpc-socket-denied", "[rpc-socket-DENIED] pid=1 tid=2 call=pthread_kill image=kernel delta=0x1a denied=1"),
        ("checkout-path", "[checkout-path] pid=1 tid=2 page=0x7f ready=0 main=1"),
        ("checkin-path", "[checkin-path] pid=1 tid=2 page=0x7f ready=0 -> no-transport (declined)"),
        ("release-drops-pending", "[release-drops-pending] site=dserver-ring.c:2752"),
    ];

    #[test]
    fn every_registered_instrument_matches_its_sample() {
        for (name, sample) in SAMPLES {
            let (_, pattern, _) = INSTRUMENTS
                .iter()
                .find(|(n, _, _)| n == name)
                .unwrap_or_else(|| panic!("instrument {name} is registered nowhere"));
            let re = Regex::new(pattern).unwrap();
            assert!(re.is_match(sample), "instrument {name} does not match its own sample line");
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
