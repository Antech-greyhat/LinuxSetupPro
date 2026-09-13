# Contributing to LinuxSetupPro

Thanks for your interest in improving LinuxSetupPro. This project values small, precise changes and honest behavior over feature count.

## Your First Contribution

New to this? Here is the whole flow, start to finish. Every step is a command you can copy and run.

1. Fork the repository — click the **Fork** button at the top right of the [GitHub page](https://github.com/Antech-greyhat/LinuxSetupPro) to create your own copy under your account.
2. Clone your fork to your machine (replace `<your-username>` with your GitHub username):

   ```bash
   git clone https://github.com/<your-username>/LinuxSetupPro.git
   cd LinuxSetupPro
   ```

3. Create a branch for your change so your work stays separate from `master` and is easy to review:

   ```bash
   git checkout -b my-change
   ```

4. Make your edit — see the sections below for exactly what to change: [Adding a new package](#adding-a-new-package), [Adding a new error pattern](#adding-a-new-error-pattern), or [Adding support for a new distribution](#adding-support-for-a-new-distribution).
5. Commit your change with a message that says what changed and why, not just "fix":

   ```bash
   git add .
   git commit -m "Add ripgrep to the Essentials category"
   ```

6. Push your branch to your fork:

   ```bash
   git push origin my-change
   ```

7. Open a pull request — GitHub shows a **Compare & pull request** button after you push; click it to send your change here for review.

## Adding a new package

No code changes are required. Edit `config/packages.yaml` and add an entry under the appropriate category:

```yaml
- name: ripgrep
  description: Fast recursive search tool that respects gitignore.
  termux: ripgrep
  apt: ripgrep
  dnf: ripgrep
  pacman: ripgrep
```

Rules:

- `name` is the logical tool name shown in reports and menus.
- `description` is one plain sentence describing what the tool does.
- Provide a package-name key per backend (`termux`, `apt`, `dnf`, `pacman`). Omit a backend entirely if the package genuinely is not available there rather than guessing a name.
- Add `locked: true` for anything that requires a proot chroot or root access to function.

Verify with a dry run before opening a PR:

```
python3 main.py --dry-run
```

## Adding a new error pattern

Edit `config/errorPatterns.yaml`. Patterns are matched against captured stderr in order, and the first match wins:

```yaml
- match: "held broken packages"
  cause: Dependencies could not be resolved because some packages are held back.
  suggestion:
    apt: Run "sudo apt --fix-broken install" and retry.
    all: Resolve the dependency conflict before retrying.
```

Use `all` for a backend-agnostic suggestion. Keep `cause` in plain language and `suggestion` actionable. Never add a pattern that fabricates a cause for a broad, generic match.

## Adding support for a new distribution

1. Implement a new backend in `core/packageManager.py` by subclassing `PackageManagerBackend` and defining `is_installed`, `install`, `upgrade`, and `remove`. Each method must return an `OperationResult` and must never raise to the caller.
2. Register the backend class in the `_BACKENDS` map in the same file.
3. Add detection for the distribution in `core/detector.py` (extend `_match_os_release` and the platform constants).
4. Add the matching backend key to any packages in `config/packages.yaml` that ship on the new distribution.

## Code style

- PEP 8.
- One-line, precise comments only, and only where the code is not self-explanatory. Explain why, not what.
- No dead code, no placeholder logic, no emojis.
- Every subprocess call must capture stdout, stderr, and the exit code, and must not abort the run on failure.

## Testing changes locally

1. Run `python3 main.py --dry-run` first to confirm nothing is broken and to see what would happen.
2. Then do a real run on your own platform to confirm the change behaves as expected.
3. Include the platform you tested on in your pull request description.
