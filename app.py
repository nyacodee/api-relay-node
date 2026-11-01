import os
import requests
from flask import Flask, request, Response

app = Flask(__name__)


def normalize_token(tok):
    t = (tok or "").strip()
    if t.lower().startswith("https://"):
        t = t[8:]
    if t.lower().startswith("http://"):
        t = t[7:]
    if t.lower().startswith("api.telegram.org/"):
        t = t[len("api.telegram.org/"):]
    if t.lower().startswith("bot"):
        t = t[3:]
    if "/" in t:
        t = t.split("/", 1)[0]
    return t.strip()


UPSTREAM_API_KEY = normalize_token(os.environ.get("UPSTREAM_API_KEY", ""))
UPSTREAM_BASE = os.environ.get("UPSTREAM_BASE", "https://api.telegram.org").rstrip("/")
UPSTREAM_TIMEOUT = int(os.environ.get("UPSTREAM_TIMEOUT", "120"))

app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024


def split_method(path):
    if path.startswith("bot/"):
        rest = path[4:]
        slash = rest.find("/")
        if slash > 0:
            return rest[slash + 1:]
        return ""
    if path.startswith("bot"):
        rest = path[3:]
        slash = rest.find("/")
        if slash > 0:
            return rest[slash + 1:]
        return ""
    return path


@app.route("/", methods=["GET", "HEAD"])
def health():
    return Response("ok", status=200, mimetype="text/plain")


@app.route("/__ping", methods=["GET"])
def __ping():
    return {
        "version": "v4-normalized",
        "target_template": f"{UPSTREAM_BASE}/bot<TOKEN>/<method>",
        "token_len": len(UPSTREAM_API_KEY),
        "token_has_colon": ":" in UPSTREAM_API_KEY,
        "token_first4": UPSTREAM_API_KEY[:4],
        "token_last4": UPSTREAM_API_KEY[-4:] if UPSTREAM_API_KEY else "",
    }


@app.route("/__routes", methods=["GET"])
def __routes():
    rules = []
    for r in app.url_map.iter_rules():
        methods = ",".join(sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS")))
        rules.append(f"{methods:25s} {r.rule}")
    return Response("\n".join(sorted(rules)), status=200, mimetype="text/plain")


@app.route("/", defaults={"path": ""}, methods=["POST", "PUT", "DELETE", "PATCH"])
@app.route("/<path:path>", methods=["POST", "PUT", "DELETE", "PATCH"])
def relay(path):
    if not path:
        return Response("ok", status=200, mimetype="text/plain")

    if not UPSTREAM_API_KEY:
        print("[RELAY] UPSTREAM_API_KEY missing", flush=True)
        return Response("UPSTREAM_API_KEY not configured", status=500,
                        mimetype="text/plain")

    method = split_method(path)
    if not method:
        print(f"[RELAY] bad path={path!r}", flush=True)
        return Response("bad path", status=400, mimetype="text/plain")

    target = f"{UPSTREAM_BASE}/bot{UPSTREAM_API_KEY}/{method}"

    safe_target = target.replace(UPSTREAM_API_KEY, "<TOKEN>")
    print(f"[RELAY] {request.method} path={path!r} method={method!r} target={safe_target}",
          flush=True)

    headers = {}
    ct = request.headers.get("Content-Type")
    if ct:
        headers["Content-Type"] = ct

    body = request.get_data(cache=False)

    try:
        resp = requests.request(
            method=request.method,
            url=target,
            data=body,
            headers=headers,
            timeout=UPSTREAM_TIMEOUT,
            allow_redirects=False,
        )
    except requests.Timeout:
        print(f"[RELAY] upstream TIMEOUT after {UPSTREAM_TIMEOUT}s", flush=True)
        return Response("upstream timeout", status=504)
    except requests.RequestException as e:
        print(f"[RELAY] upstream ERROR {e}", flush=True)
        return Response(f"upstream error: {e}", status=502)

    snippet = ""
    if resp.status_code >= 400:
        snippet = resp.content[:300].decode("utf-8", "replace")
        print(f"[RELAY] upstream -> HTTP {resp.status_code} body={snippet}", flush=True)

    return Response(
        resp.content,
        status=resp.status_code,
        content_type=resp.headers.get("Content-Type", "application/json"),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
