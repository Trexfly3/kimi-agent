"""Persistent Python execution worker for kimi-agent.

Runs as a standalone script (so it works with any interpreter, even one without kimi-agent installed).
Protocol: one JSON request per line on the original stdin ({"code", "cwd"}), one JSON response per line
on the original stdout ({"output", "ok"}). During execution file descriptors 1 and 2 are redirected to a
temporary file so output from both Python code and child processes is captured.
"""

import ast
import json
import os
import signal
import sys
import tempfile
import traceback

WORKER_FILE = os.path.abspath(__file__)


def _execute(code, namespace):
    tree = ast.parse(code, "<cell>", "exec")
    last = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        last = ast.Expression(tree.body.pop().value)
    exec(compile(tree, "<cell>", "exec"), namespace)
    if last is not None:
        value = eval(compile(last, "<cell>", "eval"), namespace)
        if value is not None:
            namespace["_"] = value
            print(repr(value))


def _report_exception():
    etype, value, tb = sys.exc_info()
    frames = [f for f in traceback.extract_tb(tb) if os.path.abspath(f.filename) != WORKER_FILE]
    text = ""
    if frames:
        text = "Traceback (most recent call last):\n" + "".join(traceback.format_list(frames))
    text += "".join(traceback.format_exception_only(etype, value))
    sys.stderr.write(text)


def _run(code, cwd, namespace):
    if cwd:
        try:
            os.chdir(cwd)
        except OSError:
            pass
    ok = True
    with tempfile.TemporaryFile() as tmp:
        sys.stdout.flush()
        sys.stderr.flush()
        saved_out, saved_err = os.dup(1), os.dup(2)
        os.dup2(tmp.fileno(), 1)
        os.dup2(tmp.fileno(), 2)
        if hasattr(signal, "SIGINT"):
            signal.signal(signal.SIGINT, signal.default_int_handler)
        try:
            _execute(code, namespace)
        except SystemExit as exc:
            print(f"SystemExit: {exc.code}")
        except BaseException:
            ok = False
            _report_exception()
        finally:
            if hasattr(signal, "SIGINT"):
                signal.signal(signal.SIGINT, signal.SIG_IGN)
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            os.dup2(saved_out, 1)
            os.dup2(saved_err, 2)
            os.close(saved_out)
            os.close(saved_err)
        tmp.seek(0)
        data = tmp.read()
    return data.decode("utf-8", "replace"), ok


def main():
    proto_in = os.fdopen(os.dup(0), "r", encoding="utf-8")
    proto_out = os.fdopen(os.dup(1), "w", encoding="utf-8")
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, signal.SIG_IGN)  # only interruptible while running a cell
    namespace = {"__name__": "__main__", "__builtins__": __builtins__}
    for line in proto_in:
        try:
            request = json.loads(line)
        except ValueError:
            continue
        try:
            output, ok = _run(request.get("code", ""), request.get("cwd"), namespace)
        except KeyboardInterrupt:
            output, ok = "KeyboardInterrupt\n", False
        proto_out.write(json.dumps({"output": output, "ok": ok}) + "\n")
        proto_out.flush()


if __name__ == "__main__":
    main()
