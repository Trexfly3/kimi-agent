"""Command risk analysis and the approval policy.

Every tool call is checked by ApprovalPolicy.evaluate() before it runs and gets one of three outcomes:
allow (run immediately), ask (the user must confirm) or deny (refused, the model is told why).

Approval modes:
  ask   - confirm everything except reading and the agent's internal bookkeeping
  auto  - (default) auto-approve read-only commands, web access and file edits inside the project directory
  yolo  - approve everything except catastrophic commands (e.g. `rm -rf ~`), which always need confirmation
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SAFE, NORMAL, DANGEROUS, CATASTROPHIC = "safe", "normal", "dangerous", "catastrophic"
RISK_ORDER = {SAFE: 0, NORMAL: 1, DANGEROUS: 2, CATASTROPHIC: 3}
COMMAND_TOOLS = {"run_command": "command", "start_process": "command", "process_input": "text"}

# --------------------------------------------------------------------------- command classification

SAFE_COMMANDS = {
    "ls", "ll", "la", "dir", "pwd", "cd", "echo", "printf", "cat", "tac", "head", "tail", "wc", "nl", "less",
    "grep", "egrep", "fgrep", "rg", "ag", "ack", "find", "fd", "locate", "which", "whereis", "type", "file",
    "stat", "du", "df", "tree", "diff", "cmp", "comm", "cut", "sort", "uniq", "tr", "column", "jq", "basename",
    "dirname", "realpath", "readlink", "md5sum", "sha1sum", "sha256sum", "sha512sum", "shasum", "cksum", "od",
    "hexdump", "strings", "seq", "true", "false", "test", "[", "sleep", "whoami", "id", "groups", "uname",
    "date", "cal", "uptime", "free", "ps", "pgrep", "nproc", "lscpu", "lsblk", "lsusb", "lspci", "arch",
    "getconf", "locale", "tty", "who", "w", "env", "printenv", "man", "whatis", "apropos", "tldr", "rev",
    "fold", "fmt", "expand", "unexpand", "paste", "join", "zcat", "bzcat", "xzcat", "dig", "nslookup", "host",
    "export", "set", "unset", "journalctl", "lsof", "vmstat", "iostat", "sw_vers", "system_profiler",
}

SAFE_SUBCOMMANDS = {
    "git": set(),  # handled by _git_safe
    "pip": {"list", "show", "freeze", "check", "help"},
    "pip3": {"list", "show", "freeze", "check", "help"},
    "npm": {"ls", "list", "view", "info", "outdated", "why", "explain", "help", "root", "prefix"},
    "pnpm": {"ls", "list", "why", "outdated"},
    "yarn": {"list", "info", "why"},
    "cargo": {"tree", "metadata", "version"},
    "go": {"version", "env", "list", "doc"},
    "docker": {"ps", "images", "version", "info", "logs", "inspect", "top", "port", "history"},
    "systemctl": {"status", "list-units", "list-unit-files", "is-active", "is-enabled", "is-failed", "show", "cat"},
    "brew": {"list", "info", "search", "config", "outdated", "deps", "leaves"},
    "apt": {"list", "show", "search", "policy"},
    "apt-cache": {"search", "show", "policy", "depends", "rdepends", "madison"},
    "gh": {"pr list", "pr view", "pr status", "pr diff", "pr checks", "issue list", "issue view", "issue status",
           "repo view", "run list", "run view", "release list", "release view", "auth status"},
}

# Flags that make otherwise read-only commands write files or run other programs.
UNSAFE_FLAGS = {
    "find": ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"),
    "fd": ("-x", "--exec", "-X", "--exec-batch"),
    "rg": ("--pre",),
    "sort": ("-o", "--output"),
    "tree": ("-o",),
    "date": ("-s", "--set"),
    "env": ("-S", "--split-string"),
}

VERSION_ARGS = {"--version", "-V", "--help", "-h", "version", "-v", "help"}

GIT_READONLY = {
    "status", "log", "diff", "show", "rev-parse", "ls-files", "ls-tree", "ls-remote", "blame", "annotate",
    "describe", "shortlog", "grep", "cat-file", "reflog", "whatchanged", "count-objects", "check-ignore",
    "check-attr", "merge-base", "name-rev", "rev-list", "show-ref", "for-each-ref", "version", "help", "var",
    "cherry", "range-diff", "diff-tree", "diff-files", "diff-index", "verify-commit", "verify-tag", "show-branch",
}

WRAPPERS = {"sudo", "doas", "env", "time", "nice", "nohup", "timeout", "command", "builtin", "exec", "xargs",
            "stdbuf", "ionice", "caffeinate", "chronic", "unbuffer"}
PRIVILEGED = {"sudo", "doas", "su", "pkexec", "runas", "gsudo"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "ash"}
_SHELL_VALUE_OPTS = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
_MAX_SHELL_NESTING = 3
WRAPPER_VALUE_OPTS = {
    "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "-T"},
    "xargs": {"-I", "-L", "-n", "-P", "-d", "-E", "-s", "-a"},
    "nice": {"-n"},
    "ionice": {"-c", "-n", "-p"},
    "timeout": {"-s", "-k", "--signal", "--kill-after"},
    "env": {"-u", "-C", "--unset", "--chdir"},
}

DANGEROUS_COMMANDS = {
    "su": "switches user", "pkexec": "runs with elevated privileges",
    "shutdown": "shuts down the machine", "reboot": "reboots the machine", "halt": "halts the machine",
    "poweroff": "powers off the machine", "kill": "kills processes", "pkill": "kills processes",
    "killall": "kills processes", "chown": "changes file ownership", "fdisk": "modifies disk partitions",
    "parted": "modifies disk partitions", "sgdisk": "modifies disk partitions", "gdisk": "modifies disk partitions",
    "mkswap": "modifies disks", "swapoff": "modifies swap", "mount": "mounts filesystems",
    "umount": "unmounts filesystems", "passwd": "changes passwords", "chpasswd": "changes passwords",
    "useradd": "modifies user accounts", "userdel": "deletes user accounts", "usermod": "modifies user accounts",
    "groupdel": "deletes groups", "visudo": "edits sudo rules", "wipefs": "wipes filesystem signatures",
    "shred": "irrecoverably destroys files", "srm": "irrecoverably destroys files",
}

_DANGEROUS_PATTERNS: list[tuple[str, str, int]] = [
    (r"\bgit\s+(?:-\S+\s+)*push\b[^;&|\n]*(?:\s-f\b|\s--force\b|\s--force-with-lease\b|\s--mirror\b|\s\+\S)",
     "force-pushes / overwrites remote history", 0),
    (r"\bgit\s+(?:-\S+\s+)*push\b[^;&|\n]*\s(?:--delete\b|-d\b|:\S)", "deletes a remote branch", 0),
    (r"\bgit\s+(?:-\S+\s+)*reset\b[^;&|\n]*--hard", "discards uncommitted changes (git reset --hard)", 0),
    (r"\bgit\s+(?:-\S+\s+)*clean\b[^;&|\n]*\s-[a-zA-Z]*f", "deletes untracked files (git clean -f)", 0),
    (r"\bgit\s+(?:-\S+\s+)*(?:checkout|restore)\s+(?:--\s+)?\.(?:\s|$)", "discards uncommitted changes", 0),
    (r"\bgit\s+(?:-\S+\s+)*branch\s+[^;&|\n]*(?:-D\b|--delete\s+--force)", "force-deletes a branch", 0),
    (r"\bgit\s+(?:-\S+\s+)*stash\s+(?:drop|clear)\b", "deletes stashed changes", 0),
    (r"\bgit\s+(?:-\S+\s+)*(?:filter-branch|filter-repo|update-ref\s+-d)\b", "rewrites git history", 0),
    (r"\b(?:curl|wget|fetch|iwr|Invoke-WebRequest)\b[^;&\n]*\|\s*(?:sudo\s+)?(?:ba|z|da|k|fi|c|tc)?sh\b",
     "pipes a download straight into a shell", re.I),
    (r"\b(?:curl|wget)\b[^;&\n]*\|\s*(?:sudo\s+)?(?:python[0-9.]*|perl|ruby|node|php)\b",
     "pipes a download straight into an interpreter", 0),
    (r"\bbase64\b[^;&\n]*(?:-d|--decode)[^;&\n]*\|\s*(?:ba|z)?sh\b", "executes encoded commands", 0),
    (r"\binit\s+[06]\b", "shuts down or reboots the machine", 0),
    (r"\bsystemctl\s+(?:poweroff|reboot|halt|suspend|hibernate|stop|disable|mask|kill|isolate)\b",
     "changes system services or power state", 0),
    (r"\bchmod\s+(?:-\S+\s+)*(?:-[a-zA-Z]*R[a-zA-Z]*|--recursive)\b", "recursively changes permissions", 0),
    (r"\bchmod\s+[^;&|\n]*(?:\b[0-7]?777\b|\b[0-7]?666\b|\ba\+w\b|\bo\+w\b)", "makes files world-writable", 0),
    (r"\bchmod\s+[^;&|\n]*(?:\bu\+s\b|\b[2-7][0-7]{3}\b)", "sets setuid/setgid bits", 0),
    (r"\bdd\b[^;&|\n]*\bof=", "writes raw data with dd", 0),
    (r"\b(?:losetup|cryptsetup|lvremove|vgremove|pvremove)\b|\bzpool\s+destroy\b|\bzfs\s+destroy\b",
     "modifies disks or volumes", 0),
    (r"\bdocker\s+(?:rm|rmi|kill|system\s+prune|volume\s+(?:rm|prune)|network\s+(?:rm|prune)|image\s+prune|"
     r"container\s+prune|builder\s+prune)\b", "removes Docker resources", 0),
    (r"\bdocker(?:-compose|\s+compose)\s+down\b[^;&|\n]*(?:\s-v\b|--volumes)", "deletes Docker volumes", 0),
    (r"\bkubectl\s+(?:delete|drain|cordon|replace\s+--force)\b", "modifies a Kubernetes cluster", 0),
    (r"\bterraform\s+(?:destroy|apply)\b|\bpulumi\s+(?:destroy|up)\b", "changes cloud infrastructure", 0),
    (r"\bhelm\s+(?:uninstall|delete|rollback)\b", "changes a Kubernetes release", 0),
    (r"\b(?:aws\s+s3\s+(?:rm|rb)|aws\s+\S+\s+delete-|gcloud\s+.*\bdelete\b|az\s+.*\bdelete\b)",
     "deletes cloud resources", 0),
    (r"\b(?:DROP\s+(?:TABLE|DATABASE|SCHEMA|USER|INDEX)|TRUNCATE\s+(?:TABLE\s+)?\w+|DELETE\s+FROM\s+\w+\s*(?:;|$|\"|'))",
     "destroys database data", re.I),
    (r"\b(?:redis-cli\b[^;&|\n]*\bFLUSH(?:ALL|DB)|dropdb\b|mongo\w*\b[^;&|\n]*dropDatabase)",
     "destroys database data", re.I),
    (r"\bcrontab\s+(?:-\S+\s+)*-r\b", "deletes the crontab", 0),
    (r"\b(?:iptables|ip6tables|nft)\s+(?:-F|--flush|flush)\b|\bufw\s+(?:disable|reset)\b",
     "changes firewall rules", 0),
    (r"\b(?:npm|pnpm|yarn|bun)\s+publish\b|\bcargo\s+publish\b|\btwine\s+upload\b|\bgem\s+push\b|"
     r"\bpoetry\s+publish\b|\bflit\s+publish\b|\buv\s+publish\b|\bgh\s+release\s+create\b",
     "publishes a package or release", 0),
    (r"\bgh\s+(?:repo\s+(?:delete|archive|edit)|secret\s+(?:set|delete)|api\b[^;&|\n]*-X\s*DELETE)",
     "modifies GitHub repository settings", 0),
    (r">>?\s*(?:~|\$HOME|/home/[^/\s]+|/Users/[^/\s]+|/root)/(?:\.bashrc|\.zshrc|\.profile|\.bash_profile|"
     r"\.zprofile|\.zshenv|\.ssh/\S*|\.gitconfig)", "modifies shell startup files or SSH configuration", 0),
    (r">>?\s*/etc/", "modifies system configuration", 0),
    (r"\b(?:nc|ncat|netcat)\b[^;&|\n]*\s-[a-zA-Z]*[ec]\b|/dev/tcp/|/dev/udp/", "opens a remote shell", 0),
    (r"(?:^|[;&|(\s])eval\s", "evaluates dynamically built code", 0),
    (r"\bhistory\s+-c\b|\bunset\s+HISTFILE\b", "clears shell history", 0),
    (r"\bsecurity\s+(?:delete|dump)-|\bkeyctl\b|\bsecret-tool\s+clear\b", "accesses the system keychain", 0),
    (r"\b(?:Remove-Item\b[^;\n]*-Recurse|Format-Volume|Clear-Disk|Stop-Computer|Restart-Computer|"
     r"reg\s+delete|bcdedit|diskpart|vssadmin\s+delete|wmic\s+shadowcopy\s+delete)",
     "destructive Windows command", re.I),
    (r"(?:^|[;&|(\s])(?:del|erase|rd|rmdir)\s+[^;&|\n]*/[sSqQ]\b", "recursively deletes files (Windows)", 0),
]
DANGEROUS_PATTERNS = [(re.compile(p, flags), reason) for p, reason, flags in _DANGEROUS_PATTERNS]

_SYSTEM_DIRS = ("bin", "boot", "dev", "etc", "home", "lib", "lib32", "lib64", "opt", "proc", "root", "sbin",
                "srv", "sys", "usr", "var", "System", "Users", "Applications", "Library", "private", "Volumes")
_CRITICAL_RE = r"(?:/|/\*|~|~/|~/\*|\$\{?HOME\}?/?\*?|/(?:" + "|".join(_SYSTEM_DIRS) + r")/?\*?|[A-Za-z]:[\\/]?)"
CATASTROPHIC_PATTERNS = [
    (re.compile(r"\brm\s+(?:-\S+\s+)*(?:-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)\s+(?:\S+\s+)*?[\"']?"
                + _CRITICAL_RE + r"[\"']?(?=\s|$|;|&|\||\))"), "recursively deletes the root or home directory"),
    (re.compile(r"--no-preserve-root"), "disables rm's root protection"),
    (re.compile(r"(?:^|[;&|(\s])mkfs(?:\.\w+)?\s"), "formats a filesystem"),
    (re.compile(r"\bdd\b[^;&|\n]*\bof=/dev/(?:sd|hd|nvme|disk|rdisk|mmcblk|vd|xvd)"), "overwrites a disk device"),
    (re.compile(r">\s*/dev/(?:sd|hd|nvme|disk|rdisk|mmcblk|vd|xvd)"), "overwrites a disk device"),
    (re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), "fork bomb"),
    (re.compile(r"\b(?:chmod|chown)\s+(?:-\S+\s+)*(?:-[a-zA-Z]*R[a-zA-Z]*|--recursive)\s+(?:\S+\s+)?"
                + _CRITICAL_RE + r"(?=\s|$|;|&|\|)"), "recursively changes permissions of system or home files"),
    (re.compile(r"\b(?:wipefs|shred)\b[^;&|\n]*/dev/"), "wipes a disk device"),
    (re.compile(r"\bfind\s+(?:/|~|\$HOME|\$\{HOME\})\s[^;&|\n]*-delete\b"), "deletes files across the system"),
    (re.compile(r"\bmv\s+(?:-\S+\s+)*(?:/|~|\$HOME|\$\{HOME\})(?:\s|/\s)"), "moves the root or home directory"),
    (re.compile(r"(?:^|[;&|(\s])format\s+[a-zA-Z]:", re.I), "formats a drive (Windows)"),
]

_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_OPERATOR_CHARS = "();<>|&\n"
_HARMLESS_TARGETS = {"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty", "nul", "NUL"}


@dataclass
class Segment:
    words: list[str]
    writes: bool = False


@dataclass
class CommandAnalysis:
    risk: str
    reason: str = ""
    keys: list[str] = field(default_factory=list)  # prefixes of the non-safe segments (for "always allow")
    segments: list[Segment] = field(default_factory=list)


def split_segments(command: str) -> list[Segment] | None:
    """Split a shell command line into simple commands. Returns None if it cannot be parsed."""
    try:
        lexer = shlex.shlex(command.replace("\\\n", " "), posix=True, punctuation_chars=_OPERATOR_CHARS)
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    segments: list[Segment] = []
    current = Segment([])
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok and all(c in _OPERATOR_CHARS for c in tok):
            if "<" in tok or ">" in tok:
                target = tokens[i + 1] if i + 1 < len(tokens) else ""
                if ">" in tok and not (tok.endswith("&") and (target.isdigit() or target == "-")) \
                        and target not in _HARMLESS_TARGETS:
                    current.writes = True
                i += 2
                continue
            if current.words or current.writes:
                segments.append(current)
            current = Segment([])
            i += 1
            continue
        current.words.append(tok)
        i += 1
    if current.words or current.writes:
        segments.append(current)
    return segments


def strip_wrappers(words: list[str]) -> tuple[list[str], list[str]]:
    """Remove variable assignments and wrapper commands (sudo, env, xargs...). Returns (wrappers, command)."""
    wrappers: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if _ASSIGN_RE.match(word):
            i += 1
            continue
        base = os.path.basename(word)
        if base not in WRAPPERS:
            break
        wrappers.append(base)
        i += 1
        value_opts = WRAPPER_VALUE_OPTS.get(base, set())
        while i < len(words) and words[i].startswith("-") and words[i] != "--":
            option = words[i]
            i += 1
            if option in value_opts and i < len(words):
                i += 1
        if i < len(words) and words[i] == "--":
            i += 1
        if base == "timeout" and i < len(words) and re.match(r"^\d", words[i]):
            i += 1
    return wrappers, words[i:]


def shell_script(words: list[str]) -> tuple[list[str], str] | None:
    """Split ``[wrappers] bash [options] -c SCRIPT [args]`` into (the wrapper words, SCRIPT)."""
    _, rest = strip_wrappers(words)
    if not rest or os.path.basename(rest[0]) not in SHELLS:
        return None
    i = 1
    while i < len(rest) and rest[i][:1] in ("-", "+") and rest[i] not in ("-", "--", "+"):
        option = rest[i]
        i += 1
        if option in _SHELL_VALUE_OPTS:
            i += 1
        elif not option.startswith("--") and "c" in option[1:]:
            return (words[:len(words) - len(rest)], rest[i]) if i < len(rest) else None
    return None


def _has_flag(args: list[str], flags: tuple[str, ...]) -> bool:
    for arg in args:
        for flag in flags:
            if arg == flag or (flag.startswith("--") and arg.startswith(flag + "=")):
                return True
            if len(flag) == 2 and flag[1].isalpha() and arg.startswith("-") and not arg.startswith("--") \
                    and len(arg) > 2 and flag[1] in arg[1:]:
                return True  # combined short flags, e.g. sort -no out
    return False


def _git_safe(args: list[str]) -> bool:
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] in ("-c", "--config-env", "--exec-path"):
            return False  # config overrides can run arbitrary programs (core.pager, core.fsmonitor...)
        i += 2 if args[i] in ("-C", "--git-dir", "--work-tree", "--namespace") else 1
    if i >= len(args):
        return True
    sub, rest = args[i], args[i + 1:]
    if any(a.startswith("--output") or a.startswith("--ext-diff") for a in rest):
        return False
    if sub in GIT_READONLY:
        return not (sub == "reflog" and rest and rest[0] in ("expire", "delete"))
    if sub == "branch":
        return all(a in ("-a", "-r", "-v", "-vv", "--all", "--remotes", "--list", "-l", "--show-current",
                         "--verbose", "--merged", "--no-merged", "--contains", "--no-color", "--color") for a in rest)
    if sub == "tag":
        return not rest or rest[0] in ("-l", "--list", "-n")
    if sub == "remote":
        return not rest or rest[0] in ("-v", "--verbose", "show", "get-url")
    if sub == "stash":
        return bool(rest) and rest[0] in ("list", "show")
    if sub == "config":
        return any(a in ("--get", "--get-all", "--get-regexp", "--list", "-l") for a in rest)
    if sub in ("worktree", "submodule"):
        return not rest or rest[0] in ("list", "status")
    return False


def _is_safe(name: str, args: list[str]) -> bool:
    if len(args) == 1 and args[0] in VERSION_ARGS:
        return True
    if name == "git":
        return _git_safe(args)
    if name in SAFE_SUBCOMMANDS:
        allowed = SAFE_SUBCOMMANDS[name]
        positional = [a for a in args if not a.startswith("-")]
        return bool(positional) and (positional[0] in allowed or " ".join(positional[:2]) in allowed)
    if name in SAFE_COMMANDS:
        return not _has_flag(args, UNSAFE_FLAGS.get(name, ()))
    return False


def command_key(words: list[str]) -> str:
    """Prefix used for "always allow" rules, e.g. 'npm install', 'npm run test', 'pytest'."""
    _, rest = strip_wrappers(words)
    if not rest:
        return ""
    name = os.path.basename(rest[0])
    positional = [a for a in rest[1:] if not a.startswith("-")]
    multi = {"git", "npm", "pnpm", "yarn", "bun", "cargo", "go", "docker", "kubectl", "pip", "pip3", "uv",
             "poetry", "gh", "brew", "apt", "apt-get", "dnf", "yum", "pacman", "systemctl", "conda", "mvn",
             "gradle", "dotnet", "rustup", "deno", "terraform", "helm", "aws", "gcloud", "az", "npx", "make",
             "just", "hatch", "pdm", "rye", "mise", "nix", "flutter", "dart", "swift", "composer", "bundle",
             "rake", "rails", "php", "artisan"}
    if name in multi and positional:
        sub = positional[0]
        if name in ("npm", "pnpm", "yarn", "bun", "uv", "poetry", "pdm", "hatch") and sub in ("run", "exec", "x") \
                and len(positional) > 1:
            return f"{name} {sub} {positional[1]}"
        return f"{name} {sub}"
    return name


def _home_variants() -> set[str]:
    home = str(Path.home())
    return {home, home + "/", home + "/*"}


def _rm_risk(args: list[str]) -> tuple[str, str]:
    recursive = force = False
    targets: list[str] = []
    end_of_options = False
    for arg in args:
        if not end_of_options and arg == "--":
            end_of_options = True
        elif not end_of_options and arg.startswith("--"):
            recursive |= arg == "--recursive"
            force |= arg == "--force"
        elif not end_of_options and arg.startswith("-") and len(arg) > 1:
            recursive |= "r" in arg or "R" in arg
            force |= "f" in arg
        else:
            targets.append(arg)
    critical = {"/", "/*", "~", "~/", "~/*", "$HOME", "$HOME/", "$HOME/*", "${HOME}", "${HOME}/", "${HOME}/*"}
    critical |= {f"/{d}" for d in _SYSTEM_DIRS} | {f"/{d}/" for d in _SYSTEM_DIRS} | {f"/{d}/*" for d in _SYSTEM_DIRS}
    critical |= _home_variants()
    if recursive and any(t in critical or re.fullmatch(r"[A-Za-z]:[\\/]?\*?", t) for t in targets):
        return CATASTROPHIC, "recursively deletes the root, a system directory or the home directory"
    if recursive or force:
        return DANGEROUS, "deletes files (rm -r/-f)"
    return NORMAL, "deletes files"


def _classify_segment(segment: Segment) -> tuple[str, str]:
    wrappers, rest = strip_wrappers(segment.words)
    privileged = [w for w in wrappers if w in PRIVILEGED]
    if not rest:
        if privileged:
            return DANGEROUS, f"runs with elevated privileges ({privileged[0]})"
        return (NORMAL, "redirects output to a file") if segment.writes else (SAFE, "")
    name = os.path.basename(rest[0])
    args = rest[1:]
    if name.startswith("mkfs"):
        return CATASTROPHIC, "formats a filesystem"
    if name == "rm":
        risk, reason = _rm_risk(args)
        if privileged and RISK_ORDER[risk] < RISK_ORDER[DANGEROUS]:
            risk, reason = DANGEROUS, f"runs with elevated privileges ({privileged[0]})"
        return risk, reason
    if privileged or name in PRIVILEGED:
        return DANGEROUS, f"runs with elevated privileges ({privileged[0] if privileged else name})"
    if name in DANGEROUS_COMMANDS:
        return DANGEROUS, DANGEROUS_COMMANDS[name]
    if segment.writes:
        return NORMAL, "redirects output to a file"
    if _is_safe(name, args):
        return SAFE, ""
    return NORMAL, ""


def _has_substitution(text: str) -> bool:
    return "$(" in text or "`" in text or "<(" in text or ">(" in text


def analyze_command(command: str, _depth: int = 0) -> CommandAnalysis:
    text = command.strip()
    if not text:
        return CommandAnalysis(SAFE)
    for pattern, reason in CATASTROPHIC_PATTERNS:
        if pattern.search(text):
            return CommandAnalysis(CATASTROPHIC, reason)
    segments = split_segments(text)
    risk, reason = SAFE, ""
    keys: list[str] = []
    if segments is None:
        risk, reason = NORMAL, "could not parse the command"
    else:
        # `bash -c "a; b"` is judged by its commands a and b, which keep the outer wrappers (sudo...) and redirects.
        expanded: list[Segment] = []
        for segment in segments:
            nested = shell_script(segment.words) if _depth < _MAX_SHELL_NESTING else None
            inner = analyze_command(nested[1], _depth + 1) if nested is not None else None
            if nested is None or inner is None or not inner.segments or _has_substitution(nested[1]):
                expanded.append(segment)
                if inner is not None and RISK_ORDER[inner.risk] > RISK_ORDER[risk]:
                    risk, reason = inner.risk, inner.reason
                continue
            if RISK_ORDER[inner.risk] > RISK_ORDER[risk]:
                risk, reason = inner.risk, inner.reason
            expanded.extend(Segment(nested[0] + sub.words, sub.writes or segment.writes) for sub in inner.segments)
        segments = expanded
        for segment in segments:
            seg_risk, seg_reason = _classify_segment(segment)
            if RISK_ORDER[seg_risk] > RISK_ORDER[risk]:
                risk, reason = seg_risk, seg_reason
            if seg_risk != SAFE:
                key = command_key(segment.words)
                if key and key not in keys:
                    keys.append(key)
        if risk == CATASTROPHIC:
            return CommandAnalysis(risk, reason, keys, segments)
    for pattern, pattern_reason in DANGEROUS_PATTERNS:
        if pattern.search(text):
            if RISK_ORDER[risk] < RISK_ORDER[DANGEROUS]:
                risk, reason = DANGEROUS, pattern_reason
            break
    if risk == SAFE and _has_substitution(text):
        risk, reason = NORMAL, "uses command substitution"
    return CommandAnalysis(risk, reason, keys, segments or [])


def classify_command(command: str) -> tuple[str, str]:
    analysis = analyze_command(command)
    return analysis.risk, analysis.reason


# --------------------------------------------------------------------------- approval policy

ALLOW, ASK, DENY = "allow", "ask", "deny"

SENSITIVE_HOME = (
    ".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".config/gcloud", ".config/gh", ".netrc", ".npmrc",
    ".pypirc", ".git-credentials", ".gitconfig", ".config/git", ".bashrc", ".bash_profile", ".bash_login",
    ".bash_logout", ".profile", ".zshrc", ".zprofile", ".zshenv", ".zlogin", ".config/fish", ".password-store",
    ".local/share/keyrings", ".config/autostart", ".config/systemd", "Library/Keychains", "Library/LaunchAgents",
)
SENSITIVE_ROOTS = ("/etc", "/usr", "/bin", "/sbin", "/boot", "/lib", "/lib64", "/opt", "/var", "/System",
                   "/Library", "/private", "C:\\Windows", "C:\\Program Files", "C:\\Program Files (x86)")


@dataclass
class Decision:
    action: str  # allow | ask | deny
    reason: str = ""
    risk: str = NORMAL
    always: str = ""  # what "always allow" would cover; empty when not offered
    keys: list[str] = field(default_factory=list)  # command prefixes remembered by "always allow"


def _is_within(path: Path, base: Path) -> bool:
    try:
        path.relative_to(base)
        return True
    except ValueError:
        return False


def sensitive_reason(path: Path, data_dir: Path | None = None) -> str | None:
    """Why writing to `path` deserves extra care (credentials, startup files, system dirs), or None."""
    home = Path(os.path.realpath(Path.home()))
    for rel in SENSITIVE_HOME:
        if _is_within(path, home / rel):
            return "it holds credentials or shell/startup configuration"
    for root in SENSITIVE_ROOTS:
        if _is_within(path, Path(root)):
            return f"it is a system location ({root})"
    if data_dir is not None and _is_within(path, data_dir):
        return "it is kimi-agent's own configuration directory"
    if ".git" in path.parts:
        return "it is inside a .git directory (hooks there can run code)"
    return None


class ApprovalPolicy:
    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.allowed_tools: set[str] = set()
        self.allowed_prefixes: set[str] = set()

    @property
    def mode(self) -> str:
        return self.settings.approval

    def evaluate(self, tool: Any, args: dict[str, Any], ctx: Any) -> Decision:
        if tool.name in COMMAND_TOOLS:
            return self._command(tool, args)
        category = tool.category
        if category in ("read", "internal"):
            return Decision(ALLOW, risk=SAFE)
        if category == "write":
            return self._write(tool, args, ctx)
        if self.mode == "yolo" or tool.name in self.allowed_tools:
            return Decision(ALLOW)
        always = f"all {tool.name} calls"
        if category == "network":
            if self.mode == "auto":
                return Decision(ALLOW)
            return Decision(ASK, "accesses the internet", NORMAL, always)
        if category == "external":
            if tool.trusted:
                return Decision(ALLOW)
            hints = tool.hints or {}
            if self.mode == "auto" and hints.get("readOnlyHint") is True and not hints.get("destructiveHint"):
                return Decision(ALLOW)
            return Decision(ASK, "calls a third-party MCP tool", NORMAL, always)
        if category == "gui":
            return Decision(ASK, "controls the mouse, keyboard or clipboard", NORMAL, always)
        if category == "execute":
            return Decision(ASK, "runs code", NORMAL, always)
        return Decision(ASK, "", NORMAL, always)

    def _command(self, tool: Any, args: dict[str, Any]) -> Decision:
        command = str(args.get(COMMAND_TOOLS[tool.name], ""))
        for pattern in self.settings.deny_commands:
            try:
                if re.search(pattern, command):
                    return Decision(DENY, f"the command matches the deny_commands pattern {pattern!r}", DANGEROUS)
            except re.error:
                continue
        analysis = analyze_command(command)
        if analysis.risk == CATASTROPHIC:
            return Decision(ASK, analysis.reason, CATASTROPHIC)
        if self.mode == "yolo":
            return Decision(ALLOW, risk=analysis.risk)
        if analysis.risk == DANGEROUS:
            return Decision(ASK, analysis.reason, DANGEROUS)
        if tool.name == "process_input":
            if tool.name in self.allowed_tools or (self.mode == "auto" and analysis.risk == SAFE):
                return Decision(ALLOW, risk=analysis.risk)
            return Decision(ASK, "sends input to a background process", NORMAL, "all process_input calls")
        if self._prefix_allowed(analysis):
            return Decision(ALLOW, risk=analysis.risk)
        if self.mode == "auto" and analysis.risk == SAFE:
            return Decision(ALLOW, risk=SAFE)
        keys = analysis.keys
        always = (", ".join(f"`{k}`" for k in keys) + " commands") if keys else ""
        return Decision(ASK, analysis.reason or "runs a shell command", analysis.risk, always, keys)

    def _prefix_allowed(self, analysis: CommandAnalysis) -> bool:
        prefixes = [p.strip() for p in self.settings.allow_commands if p.strip()] + sorted(self.allowed_prefixes)
        if not prefixes or not analysis.segments:
            return False
        for segment in analysis.segments:
            if _classify_segment(segment)[0] == SAFE:
                continue
            _, rest = strip_wrappers(segment.words)
            text = " ".join(rest)
            if not any(text == p or text.startswith(p + " ") for p in prefixes):
                return False
        return True

    def _write(self, tool: Any, args: dict[str, Any], ctx: Any) -> Decision:
        if self.mode == "yolo":
            return Decision(ALLOW)
        raw = args.get("path")
        path = ctx.resolve(raw) if raw else ctx.cwd
        real = Path(os.path.realpath(path))
        sensitive = sensitive_reason(real, Path(os.path.realpath(ctx.data_dir)))
        if sensitive:
            return Decision(ASK, f"writes to {path}: {sensitive}", DANGEROUS)
        if tool.name in self.allowed_tools:
            return Decision(ALLOW)
        always = f"all {tool.name} calls"
        if tool.name == "delete_path":
            if args.get("recursive"):
                return Decision(ASK, "recursively deletes a directory", DANGEROUS)
            return Decision(ASK, "deletes a file", NORMAL, always)
        if self.mode == "auto":
            if self.inside_root(real, ctx):
                return Decision(ALLOW)
            return Decision(ASK, f"writes outside the project directory ({ctx.root})", NORMAL, always)
        return Decision(ASK, "modifies files", NORMAL, always)

    @staticmethod
    def inside_root(path: Path, ctx: Any) -> bool:
        root = Path(os.path.realpath(ctx.root))
        if root == Path(root.anchor) or root == Path(os.path.realpath(Path.home())):
            return False  # too broad to auto-approve writes
        return _is_within(path, root)

    def remember(self, tool: Any, decision: Decision) -> None:
        """The user chose "always allow" for this kind of call (for the rest of the session)."""
        if decision.keys:
            self.allowed_prefixes.update(decision.keys)
        else:
            self.allowed_tools.add(tool.name)

    def describe(self) -> str:
        parts = [f"approval mode: {self.mode}"]
        if self.allowed_tools:
            parts.append("always allowed tools: " + ", ".join(sorted(self.allowed_tools)))
        if self.allowed_prefixes or self.settings.allow_commands:
            parts.append("allowed commands: " + ", ".join(sorted(self.allowed_prefixes | set(self.settings.allow_commands))))
        return "\n".join(parts)
