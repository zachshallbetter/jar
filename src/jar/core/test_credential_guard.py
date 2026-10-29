#!/usr/bin/env python3
"""
Tests for the credential guard: titan's own keys and tokens never reach a model or a reply.
Host file tools refuse credential paths, every tool result is scrubbed of live credential values
and private-key blocks, and the final reply is scrubbed too. Household secrets in the vault are
untouched. Hermetic: uses a temporary token file and environment.

Run: python3 scripts/test_credential_guard.py
"""
import os
import sys
import tempfile
sys.path.insert(0, os.path.dirname(__file__))

import agent_runtime_lib as lib


def check(label, got, expected):
    status = "PASS" if got == expected else "FAIL"
    print(f"  [{status}] {label}: got={got!r} expected={expected!r}")
    return got == expected


TOKEN = "92bf5b66" + "0" * 48 + "c2228358"
API_KEY = "sk-test-" + "A1b2" * 8
PEM = "-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIFAKEFAKEFAKE\n-----END PRIVATE KEY-----"


def run():
    ok = True
    tmp = tempfile.mkdtemp()
    tok_file = os.path.join(tmp, "agent_api_token.txt")
    open(tok_file, "w").write(TOKEN + "\n")
    saved = {k: os.environ.get(k) for k in ("AGENT_API_TOKEN_FILE", "SOME_PROVIDER_API_KEY")}
    os.environ["AGENT_API_TOKEN_FILE"] = tok_file
    os.environ["SOME_PROVIDER_API_KEY"] = API_KEY
    lib._credential_cache = (0.0, frozenset())
    try:
        print("\n── credential paths are refused ──")
        for path in ("/ai/secrets/coordinator_ed25519_private.pem", "/ai/secrets", "/ai/config/agent_api_token.txt",
                     "/ai/projects/titan-local-agent/agent.env", "/ai/projects/runtimes/x/service.env",
                     "/home/zach/.ssh/id_ed25519", "/tmp/anything.pem", "/etc/shadow", "/srv/app/.env.local"):
            ok &= check(f"credential path: {path}", lib._is_credential_path(path), True)
        for path in ("/ai/config/qwen3coder-mode.env", "/ai/projects/titan-node/docs/runbook.md",
                     "/ai/config/coordinator_ed25519_public.pem.txt", "/home/zach/notes.txt"):
            ok &= check(f"ordinary path: {path}", lib._is_credential_path(path), False)
        for tool in ("file_read", "file_list", "file_write", "file_edit"):
            out = lib.execute_tool(tool, {"path": "/ai/config/agent_api_token.txt", "content": "x",
                                          "old_string": "a", "new_string": "b"})
            ok &= check(f"{tool} refuses the token file", out.startswith("Refused: that path holds titan's credentials"), True)
            ok &= check(f"{tool} points to pairing, not the file", "People & devices" in out and "no cat, ssh or scp" in out, True)

        print("\n── tool results and replies never carry a live credential ──")
        leaked = f"token: {TOKEN}\nkey={API_KEY}\n{PEM}\nwifi password is hunter22"
        clean = lib.redact_credentials(leaked)
        ok &= check("agent token removed", TOKEN in clean, False)
        ok &= check("credential env value removed", API_KEY in clean, False)
        ok &= check("private key block removed", "PRIVATE KEY" in clean, False)
        ok &= check("household text untouched", "wifi password is hunter22" in clean, True)
        # a tool whose result contains the token (e.g. run_python reading the file) is scrubbed on the way out
        orig = lib._execute_tool
        lib._execute_tool = lambda name, args: f"The file says {TOKEN}"
        try:
            ok &= check("execute_tool scrubs any result", lib.execute_tool("python_eval", {}), "The file says [redacted: credential]")
        finally:
            lib._execute_tool = orig
        reply = lib._with_credentials_removed(f"Here it is: {TOKEN}")
        ok &= check("a reply with the token is redacted", TOKEN in reply, False)
        ok &= check("... and says how to connect a device instead", "pair it from Jellybean" in reply, True)
        ok &= check("a normal reply is unchanged", lib._with_credentials_removed("It's 73°F inside."), "It's 73°F inside.")
        ok &= check("short env values are not treated as credentials", "abc" in lib.redact_credentials("abc"), True)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        lib._credential_cache = (0.0, frozenset())

    print()
    print("All tests passed." if ok else "Some tests FAILED.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
