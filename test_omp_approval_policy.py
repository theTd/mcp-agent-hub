"""engine_policies/omp-approval.yml, run through a port of omp's own bash approval matcher.

The matcher below is ported line by line from the omp.exe bundle (build of 2026-11-12,
packages/coding-agent/src/tools/bash.ts and shell-tokenize.ts): the overlay's prompt rules are
tested against the whole command and against each simple command with its quotes removed, and
omp's built-in critical list is tested against the whole command string only
(bash.allowCompoundCommands is off by default). When omp changes how it matches, update the port
before trusting these results.
"""
import re
from pathlib import Path
import unittest

OVERLAY = Path(__file__).parent / "engine_policies" / "omp-approval.yml"

# omp's built-in critical list ($ge). A match asks even when a rule says allow.
OMP_CRITICAL = [
    re.compile(r"\brm\s+(?:-\S+\s+)*(?:-[a-z]*[rRfF][a-z]*|--recursive|--force)\s+(?:-\S+\s+)*\/", re.I),
    re.compile(r"\brm\s+(?:-\S+\s+)*--no-preserve-root\b", re.I),
    re.compile(r"\bsudo\s+rm\b", re.I),
    re.compile(r"\bchmod\s+-R\s+[0-7]+\s+\/", re.I),
    re.compile(r"\bchmod\s+-R\s+[ugoa+\-=rwxXst,]+\s+\/"),
    re.compile(r"\bchown\s+-R\s+\S+\s+\/", re.I),
    re.compile(r":\(\)\s*\{\s*:\s*\|\s*:", re.I),
    re.compile(r">\s*\/dev\/sd[a-z]", re.I),
    re.compile(r"\bmkfs(\.|\b)", re.I),
    re.compile(r"\bdd\s+if=.+of=\/dev\/(?!(?:null|zero|stdout|stderr|tty|fd\/\d+)(?:[\s;&|)<>]|$))", re.I),
    re.compile(r"\bshred\s+\/dev\/", re.I),
    re.compile(r"\bcryptsetup\b", re.I),
    re.compile(r">\s*\/etc\/(?:passwd|shadow|sudoers)\b", re.I),
    re.compile(r"\btee\s+(?:-a\s+)?\/etc\/(?:passwd|shadow|sudoers)\b", re.I),
    re.compile(r"\b(?:curl|wget|fetch)\b[^|]*\|\s*(?:bash|sh|zsh|fish)\b", re.I),
    re.compile(r"(?:^|[\s;&|(])(?:bash|sh|zsh|source|\.)\s+<\(\s*(?:curl|wget|fetch)\b", re.I),
    re.compile(r"\beval\s+[\"'`]?\$\(\s*(?:curl|wget|fetch)\b|\beval\s+`\s*(?:curl|wget|fetch)\b", re.I),
    re.compile(r"\bkill\s+-9\s+1\b"),
    re.compile(r"(?:^|[\s;&|(])(?:shutdown|poweroff|reboot|halt)(?:\s|$|[;|&])", re.I),
    re.compile(r"(?:^|[\s;&|(])init\s+0\b", re.I),
    re.compile(r"\bnc\b[^|;]*\s-[a-zA-Z]*[ec][a-zA-Z]*\s", re.I),
]
_META = set("\n\r;&|<>`$()")
_WRAPPER_FLAG = re.compile(r"(?:^|[ \t])(?:-[^-]*[ce]|--(?:command|eval))(?:[= \t]|$)")
_SEPARATORS = set(" \t\n;&|()")


def _norm(text):
    return re.sub(r"\s+", " ", text.strip())


def _rule_regex(match):
    parts = [re.sub(r"([\\^$+?.()|\[\]{}])", r"\\\1", p) for p in _norm(match).split("*")]
    return re.compile(".*".join(parts))


def _rule_matches_whole(text, match):
    norm = _norm(text)
    return bool(norm) and _rule_regex(match).fullmatch(norm) is not None


def _tokens(text):
    """shell-tokenize.ts: words (quotes removed) and the separator runs between them."""
    out, value, word_start, sep_start = [], "", -1, -1
    single = double = False
    i = 0

    def end_word(at):
        nonlocal value, word_start
        if word_start >= 0:
            out.append(("word", text[word_start:at], value))
            value, word_start = "", -1

    def start_word(at):
        nonlocal sep_start, word_start
        if word_start >= 0:
            return
        if sep_start >= 0:
            out.append(("separator", text[sep_start:at], None))
            sep_start = -1
        word_start = at

    while i < len(text):
        ch = text[i]
        if single:
            if ch == "'":
                single = False
            else:
                value += ch
            i += 1
            continue
        if double:
            if ch == "\\" and i + 1 < len(text) and text[i + 1] in '"\\$`':
                value += text[i + 1]
                i += 2
                continue
            if ch == '"':
                double = False
            else:
                value += ch
            i += 1
            continue
        if ch in _SEPARATORS:
            end_word(i)
            if sep_start < 0:
                sep_start = i
            i += 1
            continue
        start_word(i)
        if ch == "'":
            single = True
        elif ch == '"':
            double = True
        elif ch == "\\" and i + 1 < len(text):
            value += text[i + 1]
            i += 1
        else:
            value += ch
        i += 1
    end_word(len(text))
    if sep_start >= 0:
        out.append(("separator", text[sep_start:], None))
    return out


def _simple_commands(text):
    commands, argv = [], []
    for kind, raw, value in _tokens(text):
        if kind == "word":
            if value:
                argv.append(value)
        elif re.search(r"[\n;&|()]", raw) and argv:
            commands.append(" ".join(argv))
            argv = []
    if argv:
        commands.append(" ".join(argv))
    return [c for c in commands if c]


def _has_shell_syntax(text):
    quote, flagged, i = None, False, 0
    while i < len(text):
        ch = text[i]
        if quote == "'":
            if ch == "'":
                quote = None
            elif ch in _META:
                flagged = True
            i += 1
            continue
        if ch == "\\":
            if i + 1 < len(text) and text[i + 1] in _META:
                flagged = True
            i += 2
            continue
        if quote == '"':
            if ch == '"':
                quote = None
                i += 1
                continue
            if ch in "`$":
                return True
            if ch in _META:
                flagged = True
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            i += 1
            continue
        if ch in _META:
            return True
        i += 1
    return flagged and _WRAPPER_FLAG.search(text) is not None


def _rule_matches(text, rule):
    if rule["approval"] == "allow":
        return not _has_shell_syntax(text) and _rule_matches_whole(text, rule["match"])
    regex = _rule_regex(rule["match"])
    if not _norm(text):
        return False
    if regex.fullmatch(_norm(text)):
        return True
    return any(regex.fullmatch(c) for c in _simple_commands(text))


def _first_rule(text, rules):
    if not _has_shell_syntax(text):
        return next((r for r in rules if _rule_matches(text, r)), None)
    simple = _simple_commands(text)
    found = None
    for rule in rules:
        if (rule["approval"] != "allow" and _rule_matches_whole(text, rule["match"])
                and not any(_rule_matches_whole(c, rule["match"]) for c in simple)):
            if rule["approval"] == "deny":
                return rule
            found = found or rule
    for command in simple:
        rule = next((r for r in rules if r["approval"] != "allow" and _rule_matches_whole(command, r["match"])), None)
        if rule and rule["approval"] == "deny":
            return rule
        if rule and rule["approval"] == "prompt":
            found = found or rule
    return found


def omp_decision(command, rules):
    """(kind, rule): kind is deny / prompt / critical / allow / unmatched, as omp decides it."""
    rule = _first_rule(command, rules)
    if rule and rule["approval"] in ("deny", "prompt"):
        return rule["approval"], rule["match"]
    critical = next((p for p in OMP_CRITICAL if command and p.search(command)), None)
    if critical is not None:
        return "critical", critical.pattern
    return ("allow", rule["match"]) if rule else ("unmatched", None)


def load_overlay_rules(path=OVERLAY):
    text = path.read_text(encoding="utf-8")
    rules = [{"match": _norm(m.group(1).replace('\\"', '"').replace("\\\\", "\\")), "approval": m.group(2)}
             for m in re.finditer(r'-\s*match:\s*"((?:[^"\\]|\\.)*)"\s*\n\s*approval:\s*(\w+)', text)]
    assert len(rules) == text.count("- match:"), "every overlay rule must be a quoted match + approval pair"
    return rules


def asks(command, rules):
    return omp_decision(command, rules)[0] in ("prompt", "critical", "deny")


class OmpApprovalOverlayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = load_overlay_rules()

    def test_real_shutdowns_still_ask(self):
        for command in (
            "shutdown /s /t 0",
            "cmd /c shutdown -r",
            'powershell -Command "shutdown /s"',
            'bash -c "shutdown now"',
            "sudo poweroff",
            "shutdown.exe /s /t 0",
            "C:/Windows/System32/shutdown.exe /r /t 0",
            'cmd.exe /c "shutdown /r /t 0"',
            "powershell.exe -NoProfile -Command shutdown /s /t 0",
            'pwsh -c "shutdown /s"',
            "bash -lc 'shutdown -h now'",
            "sh -c 'shutdown -h now'",
            "cd x && shutdown /s",
            "echo y | shutdown /s",
            "sudo shutdown -h now",
            'powershell -c "shutdown.exe /r"',
            "Stop-Computer -Force",
            'powershell -Command "Restart-Computer"',
        ):
            with self.subTest(command=command):
                self.assertTrue(asks(command, self.rules), omp_decision(command, self.rules))

    def test_shutdown_in_prose_does_not_ask(self):
        # The four prompts of 2026-11-13 (docs/omp-approval-prompts-handoff.md) had these shapes; the
        # old "* shutdown*" rule asked for all of them. A bare word between spaces still asks
        # through omp's own critical rule (e.g. grep -n shutdown x), which the overlay cannot change.
        for command in (
            'grep -n "fn reaper_body\\|fn shutdown\\|fn stats" src/core.rs',
            'grep -n "fn push\\|fn shutdown_does" tests/session_lifecycle.rs',
            'grep -rn "shutdown(" .',
            "rg -n 'fn shutdown' src",
            "cd x && cat > probe.py <<'PY'\nc.shutdown()\nprint(\"shutdown %.3f s\" % t)\nPY\npy -3 probe.py",
            "python -c \"import x; x.shutdown()\"",
            "pytest -k test_shutdown",
        ):
            with self.subTest(command=command):
                self.assertFalse(asks(command, self.rules), omp_decision(command, self.rules))

    def test_recursive_deletes_still_ask(self):
        for command in (
            "rm -rf /d/tmp/elsewhere",
            "rm -rf $DIR/",
            "rm -rf ~",
            "rm -rf ../x",
            "rm -rf .",
            "cd wt && rm -rf ../scratch/bitetest && mkdir -p ../scratch/bitetest",
            "Remove-Item -Recurse -Force x",
            "cmd /c rd /s /q x",
            "git reset --hard HEAD~1",
            "git push --force origin main",
        ):
            with self.subTest(command=command):
                self.assertTrue(asks(command, self.rules), omp_decision(command, self.rules))

    def test_ordinary_commands_run(self):
        for command in ("git status", "ls -la scratch", "py -3 -m unittest test_x", "rm scratch/a.txt",
                        "ruff format .", "curl -s https://example.com"):
            with self.subTest(command=command):
                self.assertFalse(asks(command, self.rules), omp_decision(command, self.rules))

    def test_port_follows_omp_on_quotes_and_wrappers(self):
        # Spot checks of the port itself, against decisions read from omp.exe's code.
        rules = [{"match": "* shutdown*", "approval": "prompt"}]
        self.assertEqual(omp_decision('bash -c "shutdown now"', rules)[0], "prompt")  # simple command
        self.assertEqual(omp_decision('bash -c "shutdown now"', [])[0], "unmatched")  # "\"" is no boundary
        self.assertEqual(omp_decision("grep -n shutdown x", [])[0], "critical")
        self.assertEqual(_simple_commands('cd a && rm -rf "b c"; ls'), ["cd a", "rm -rf b c", "ls"])


if __name__ == "__main__":
    unittest.main()
