import os
import json
import requests
from urllib.parse import parse_qs, urlencode
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
AUTO_PARSE_MODE = os.environ.get("AUTO_PARSE_MODE", "HTML")

app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024

PARSE_MODE_METHODS = {
    "sendPhoto",
    "sendDocument",
}


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


def _inject_form(body):
    try:
        params = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    except Exception:
        return body
    if "parse_mode" in params:
        return body
    params["parse_mode"] = [AUTO_PARSE_MODE]
    try:
        return urlencode(params, doseq=True).encode("utf-8")
    except Exception:
        return body


def _inject_json(body):
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return body
    if not isinstance(data, dict):
        return body
    if "parse_mode" in data:
        return body
    data["parse_mode"] = AUTO_PARSE_MODE
    try:
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    except Exception:
        return body


def _inject_multipart(body, content_type):
    if "boundary=" not in content_type:
        return body
    try:
        boundary = content_type.split("boundary=", 1)[1].split(";", 1)[0].strip().strip('"')
    except Exception:
        return body
    if not boundary:
        return body

    if b'name="parse_mode"' in body:
        return body

    bb = boundary.encode()
    new_part = (
        b"--" + bb + b"\r\n"
        b'Content-Disposition: form-data; name="parse_mode"\r\n'
        b"\r\n" + AUTO_PARSE_MODE.encode() + b"\r\n"
    )
    final = b"--" + bb + b"--"
    idx = body.rfind(final)
    if idx < 0:
        return body
    return body[:idx] + new_part + body[idx:]


def inject_parse_mode(body, content_type, method):
    if not AUTO_PARSE_MODE:
        return body
    if method not in PARSE_MODE_METHODS:
        return body
    if not body:
        return body

    ct = (content_type or "").lower()

    if "application/x-www-form-urlencoded" in ct:
        return _inject_form(body)
    if "application/json" in ct:
        return _inject_json(body)
    if "multipart/form-data" in ct:
        return _inject_multipart(body, content_type)
    return body


@app.route("/", methods=["GET", "HEAD"])
def health():
    return Response("ok", status=200, mimetype="text/plain")


@app.route("/__ping", methods=["GET"])
def __ping():
    return {
        "version": "v5-parse-mode",
        "target_template": f"{UPSTREAM_BASE}/bot<TOKEN>/<method>",
        "token_len": len(UPSTREAM_API_KEY),
        "auto_parse_mode": AUTO_PARSE_MODE,
        "parse_mode_methods": sorted(PARSE_MODE_METHODS),
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
        return Response("UPSTREAM_API_KEY not configured", status=500,
                        mimetype="text/plain")

    method = split_method(path)
    if not method:
        return Response("bad path", status=400, mimetype="text/plain")

    target = f"{UPSTREAM_BASE}/bot{UPSTREAM_API_KEY}/{method}"

    headers = {}
    ct = request.headers.get("Content-Type")
    if ct:
        headers["Content-Type"] = ct

    body = request.get_data(cache=False)
    body = inject_parse_mode(body, ct, method)

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
        return Response("upstream timeout", status=504)
    except requests.RequestException as e:
        return Response(f"upstream error: {e}", status=502)

    return Response(
        resp.content,
        status=resp.status_code,
        content_type=resp.headers.get("Content-Type", "application/json"),
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
