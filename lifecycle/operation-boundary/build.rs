use std::env;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

const SEMANTIC_CLOSURE: &[&str] = &[
    "build.rs",
    "Cargo.toml",
    "Cargo.lock",
    "src/lib.rs",
    "src/state.rs",
    "src/explorer.rs",
    "src/fuzz.rs",
    "src/bin/lifecycle-fuzz.rs",
    "src/bin/lifecycle-boundary.rs",
    "fuzz/Cargo.toml",
    "fuzz/Cargo.lock",
    "fuzz/fuzz_targets/lifecycle.rs",
    "../../lifecycle/operation-boundary-v1.json",
    "../../lifecycle/state-model-v1.json",
    "../../docs/lifecycle-fuzzing-v1.md",
    "../../tests/west_test_contracts/lifecycle_fuzz_contract.py",
    "../../tests/west_test_contracts/lifecycle_explorer_contract.py",
    "../../tests/run-lifecycle-fuzz-contract.sh",
    "../../tests/run-lifecycle-fuzz-ub-gate.sh",
];

fn sha256(path: &Path) -> String {
    let output = Command::new("sha256sum")
        .arg(path)
        .output()
        .expect("sha256sum is required for fuzz source identity");
    assert!(
        output.status.success(),
        "sha256sum failed for {}",
        path.display()
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    let digest = stdout
        .split_whitespace()
        .next()
        .expect("sha256sum returned no digest");
    assert_eq!(digest.len(), 64, "sha256sum returned malformed digest");
    digest.to_string()
}

fn sha256_bytes(bytes: &[u8]) -> String {
    let mut child = Command::new("sha256sum")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .expect("sha256sum is required for closure identity");
    child
        .stdin
        .take()
        .expect("sha256sum stdin")
        .write_all(bytes)
        .expect("write closure manifest");
    let output = child.wait_with_output().expect("sha256sum output");
    assert!(
        output.status.success(),
        "sha256sum failed for closure manifest"
    );
    let stdout = String::from_utf8_lossy(&output.stdout);
    let digest = stdout.split_whitespace().next().expect("closure digest");
    assert_eq!(digest.len(), 64, "closure digest malformed");
    digest.to_string()
}

fn git_head(manifest_dir: &Path) -> String {
    let output = Command::new("git")
        .args(["-C", &manifest_dir.to_string_lossy(), "rev-parse", "HEAD"])
        .output();
    let Ok(output) = output else {
        return "UNBOUND".to_string();
    };
    if !output.status.success() {
        return "UNBOUND".to_string();
    }
    String::from_utf8_lossy(&output.stdout).trim().to_string()
}

fn main() {
    let manifest_dir = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").expect("manifest dir"));
    let out_dir = PathBuf::from(env::var_os("OUT_DIR").expect("OUT_DIR"));
    let fuzz_source = manifest_dir.join("src/fuzz.rs");
    let fuzz_target = manifest_dir.join("fuzz/fuzz_targets/lifecycle.rs");
    let mut manifest = String::new();
    let mut entries = Vec::with_capacity(SEMANTIC_CLOSURE.len());
    for relative in SEMANTIC_CLOSURE {
        let path = manifest_dir.join(relative);
        assert!(
            path.is_file(),
            "semantic closure path is missing: {relative}"
        );
        println!("cargo:rerun-if-changed={}", path.display());
        let digest = sha256(&path);
        manifest.push_str(relative);
        manifest.push('=');
        manifest.push_str(&digest);
        manifest.push('\n');
        entries.push(format!(
            "{{\"path\":\"{relative}\",\"sha256\":\"{digest}\"}}"
        ));
    }
    let manifest_path = out_dir.join("lifecycle-fuzz-semantic-closure.manifest");
    fs::write(&manifest_path, manifest.as_bytes()).expect("write closure manifest");
    let closure_digest = sha256_bytes(manifest.as_bytes());
    let closure_json = format!("[{}]", entries.join(","));

    println!(
        "cargo:rustc-env=LIFECYCLE_FUZZ_SOURCE_SHA256={}",
        sha256(&fuzz_source)
    );
    println!(
        "cargo:rustc-env=LIFECYCLE_FUZZ_TARGET_SHA256={}",
        sha256(&fuzz_target)
    );
    println!("cargo:rustc-env=LIFECYCLE_FUZZ_SEMANTIC_CLOSURE_SHA256={closure_digest}");
    println!("cargo:rustc-env=LIFECYCLE_FUZZ_SEMANTIC_CLOSURE_JSON={closure_json}");
    println!(
        "cargo:rustc-env=LIFECYCLE_FUZZ_WORKSPACE_HEAD={}",
        git_head(&manifest_dir)
    );
}
