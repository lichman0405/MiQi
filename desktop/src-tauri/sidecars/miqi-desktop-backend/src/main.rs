use std::env;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};

fn main() -> ExitCode {
    let repo_root = find_repo_root();
    let python = find_python(repo_root.as_deref());

    let mut command = Command::new(python);
    command.arg("-m").arg("miqi.cli.commands").arg("desktop-backend");
    command.args(env::args().skip(1));

    if let Some(root) = repo_root {
        command.current_dir(&root);
        prepend_pythonpath(&mut command, &root);
    }

    match command.status() {
        Ok(status) if status.success() => ExitCode::SUCCESS,
        Ok(_) => ExitCode::from(1),
        Err(_) => ExitCode::from(1),
    }
}

fn find_repo_root() -> Option<PathBuf> {
    if let Some(root) = env::var_os("MIQI_REPO_ROOT").map(PathBuf::from) {
        if is_repo_root(&root) {
            return Some(root);
        }
    }

    let mut starts = Vec::new();
    if let Ok(current_dir) = env::current_dir() {
        starts.push(current_dir);
    }
    if let Ok(current_exe) = env::current_exe() {
        if let Some(parent) = current_exe.parent() {
            starts.push(parent.to_path_buf());
        }
    }

    for start in starts {
        for ancestor in start.ancestors() {
            if is_repo_root(ancestor) {
                return Some(ancestor.to_path_buf());
            }
        }
    }

    None
}

fn is_repo_root(path: &Path) -> bool {
    path.join("pyproject.toml").is_file() && path.join("miqi").join("cli").join("commands.py").is_file()
}

fn find_python(repo_root: Option<&Path>) -> PathBuf {
    if let Some(python) = env::var_os("MIQI_DESKTOP_PYTHON").map(PathBuf::from) {
        return python;
    }

    if let Some(root) = repo_root {
        let windows_venv = root.join(".venv").join("Scripts").join("python.exe");
        if windows_venv.is_file() {
            return windows_venv;
        }

        let unix_venv = root.join(".venv").join("bin").join("python");
        if unix_venv.is_file() {
            return unix_venv;
        }
    }

    PathBuf::from("python")
}

fn prepend_pythonpath(command: &mut Command, repo_root: &Path) {
    let existing = env::var_os("PYTHONPATH").unwrap_or_default();
    let mut paths = vec![repo_root.to_path_buf()];
    paths.extend(env::split_paths(&existing));

    if let Ok(joined) = env::join_paths(paths) {
        command.env("PYTHONPATH", joined);
    }
}
