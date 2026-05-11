const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const desktopRoot = path.resolve(__dirname, "..");
const srcTauriRoot = path.join(desktopRoot, "src-tauri");
const sidecarManifest = path.join(
  srcTauriRoot,
  "sidecars",
  "miqi-desktop-backend",
  "Cargo.toml",
);
const targetDir = path.join(srcTauriRoot, "target", "sidecars");
const binariesDir = path.join(srcTauriRoot, "binaries");

function run(command, args, options = {}) {
  return execFileSync(command, args, {
    cwd: desktopRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    ...options,
  }).trim();
}

function hostTriple() {
  try {
    return run("rustc", ["--print", "host-tuple"]);
  } catch {
    const version = run("rustc", ["-Vv"]);
    const hostLine = version.split(/\r?\n/).find((line) => line.startsWith("host:"));
    if (!hostLine) throw new Error("Could not determine Rust host target triple");
    return hostLine.split(/\s+/)[1];
  }
}

const triple = hostTriple();
const extension = process.platform === "win32" ? ".exe" : "";

fs.mkdirSync(binariesDir, { recursive: true });

execFileSync(
  "cargo",
  ["build", "--release", "--manifest-path", sidecarManifest],
  {
    cwd: desktopRoot,
    stdio: "inherit",
    env: {
      ...process.env,
      CARGO_TARGET_DIR: targetDir,
    },
  },
);

const builtBinary = path.join(targetDir, "release", `miqi-desktop-backend${extension}`);
const tauriSidecar = path.join(binariesDir, `miqi-desktop-backend-${triple}${extension}`);

if (!fs.existsSync(builtBinary)) {
  throw new Error(`Expected sidecar launcher was not built: ${builtBinary}`);
}

fs.copyFileSync(builtBinary, tauriSidecar);
console.log(`Prepared Tauri dev sidecar: ${path.relative(desktopRoot, tauriSidecar)}`);
