"""
Vectorworks 2024/2025 MCP Listener - runs inside Vectorworks.

Opens a TCP socket (default 127.0.0.1:9877) and serves MCP requests using
non-blocking I/O via selectors. The generated launcher starts the listener in
dialog mode, which is modal but runs inside a normal Vectorworks script context
that can safely call the `vs` API. Background and Windows timer modes are kept
as transport-only diagnostics because real CAD handlers can deadlock outside
that script context. Foreground mode is also diagnostic only because it can
monopolize the Vectorworks UI.

INSTALL OPTIONS
  A) Quick in Vectorworks 2024 - Resource Manager > New Resource > Script,
     choose Python Script, paste the generated vw_load_listener_2024.py, run it.
  B) Persistent menu command - Tools > Plug-ins > Plug-in Manager >
     New > Menu Command, paste the generated vw_load_listener_2024.py. Then
     Tools > Workspaces > Edit Current Workspace > Menus and drag the
     new command into a menu. Click it once per VW session to start.

STOP: create a file named STOP in the stop-file folder printed at startup,
or close the document / quit Vectorworks.

CONFIG (env vars, all optional):
  VW_MCP_HOST       default 127.0.0.1
  VW_MCP_PORT       default 9877
  VW_MCP_STOP_DIR   default ~/.vectorworks-mcp
  VW_MCP_MAX_FRAME_BYTES default 16777216
  VW_MCP_MAX_PENDING_READ_BYTES default VW_MCP_MAX_FRAME_BYTES + 4096
  VW_MCP_MAX_PENDING_WRITE_BYTES default VW_MCP_MAX_FRAME_BYTES + 4096
  VW_MCP_MAX_CLIENTS default 8
  VW_MCP_CLIENT_IDLE_SECONDS default 600
  VW_MCP_MODE       win_timer | dialog | foreground | background; default dialog
  VW_MCP_DIALOG_TIMER_MS default 50
  VW_MCP_AUTH_TOKEN local protocol auth token; defaults to ~/.vectorworks-mcp/auth-token
  VW_MCP_AUTH_TOKEN_FILE override auth token file path
  VW_MCP_INSECURE_NO_AUTH set to 1 only for local diagnostics/tests
"""
try:
    import vs
except ModuleNotFoundError:
    vs = None

import io, ipaddress, json, math, os, re, selectors, socket, struct, sys, threading, time, traceback, types

__VERSION__ = "0.3.0-socket"

# === CONFIGURATION ===
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9877
DEFAULT_MAX_FRAME_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_PENDING_BUFFER_SLACK_BYTES = 4096
DEFAULT_MAX_CLIENTS = 8
DEFAULT_CLIENT_IDLE_SECONDS = 600
DEFAULT_DIALOG_TIMER_MS = 50
DEFAULT_AUTH_TOKEN_FILENAME = "auth-token"


def _env_int(name, default, min_value=None, max_value=None):
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("{name} must be an integer, got {value!r}".format(name=name, value=raw))
    if min_value is not None and value < min_value:
        raise ValueError("{name} must be >= {min}, got {value}".format(name=name, min=min_value, value=value))
    if max_value is not None and value > max_value:
        raise ValueError("{name} must be <= {max}, got {value}".format(name=name, max=max_value, value=value))
    return value


def _validate_loopback_host(host, env_name="VW_MCP_HOST"):
    normalized = str(host or "").strip() or DEFAULT_HOST
    if normalized.lower() == "localhost":
        return normalized
    try:
        if ipaddress.ip_address(normalized).is_loopback:
            return normalized
    except ValueError:
        raise ValueError("{name} must be a loopback IP address or localhost, got {value!r}".format(name=env_name, value=normalized))
    raise ValueError("{name} must be loopback-only; refusing {value!r}".format(name=env_name, value=normalized))


def _truthy_env(name):
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


def _default_state_dir():
    configured = os.environ.get("VW_MCP_STOP_DIR", "").strip()
    if configured:
        return os.path.expanduser(configured)
    return os.path.join(os.path.expanduser("~"), ".vectorworks-mcp")


def _default_auth_token_file():
    configured = os.environ.get("VW_MCP_AUTH_TOKEN_FILE", "").strip()
    if configured:
        return os.path.expanduser(configured)
    return os.path.join(_default_state_dir(), DEFAULT_AUTH_TOKEN_FILENAME)


def _read_auth_token_file():
    try:
        path = _default_auth_token_file()
        if not os.path.isfile(path):
            return ""
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except Exception:
        return ""


def _load_auth_token():
    if _truthy_env("VW_MCP_INSECURE_NO_AUTH"):
        return ""
    token = os.environ.get("VW_MCP_AUTH_TOKEN", "").strip()
    if token:
        return token
    return _read_auth_token_file()


def _auth_required_error():
    if AUTH_TOKEN or ALLOW_INSECURE_NO_AUTH:
        return ""
    return (
        "VW_MCP_AUTH_TOKEN is required for the local Vectorworks MCP protocol; "
        "run scripts/register-claude-code.ps1 or set VW_MCP_INSECURE_NO_AUTH=1 only for diagnostics"
    )


_CONFIG_ERROR = None
try:
    HOST = _validate_loopback_host(os.environ.get("VW_MCP_HOST", DEFAULT_HOST))
    PORT = _env_int("VW_MCP_PORT", DEFAULT_PORT, 1, 65535)
    MAX_FRAME_BYTES = _env_int("VW_MCP_MAX_FRAME_BYTES", DEFAULT_MAX_FRAME_BYTES, 1024, 128 * 1024 * 1024)
    MAX_PENDING_READ_BYTES = _env_int(
        "VW_MCP_MAX_PENDING_READ_BYTES",
        MAX_FRAME_BYTES + DEFAULT_MAX_PENDING_BUFFER_SLACK_BYTES,
        4096,
        256 * 1024 * 1024,
    )
    MAX_PENDING_WRITE_BYTES = _env_int(
        "VW_MCP_MAX_PENDING_WRITE_BYTES",
        MAX_FRAME_BYTES + DEFAULT_MAX_PENDING_BUFFER_SLACK_BYTES,
        4096,
        256 * 1024 * 1024,
    )
    MAX_CLIENTS = _env_int("VW_MCP_MAX_CLIENTS", DEFAULT_MAX_CLIENTS, 1, 64)
    CLIENT_IDLE_SECONDS = _env_int("VW_MCP_CLIENT_IDLE_SECONDS", DEFAULT_CLIENT_IDLE_SECONDS, 30, 86400)
    DIALOG_TIMER_MS = _env_int("VW_MCP_DIALOG_TIMER_MS", DEFAULT_DIALOG_TIMER_MS, 20, 5000)
    AUTH_TOKEN = _load_auth_token()
    ALLOW_INSECURE_NO_AUTH = _truthy_env("VW_MCP_INSECURE_NO_AUTH")
except ValueError as e:
    _CONFIG_ERROR = str(e)
    HOST = DEFAULT_HOST
    PORT = DEFAULT_PORT
    MAX_FRAME_BYTES = DEFAULT_MAX_FRAME_BYTES
    MAX_PENDING_READ_BYTES = DEFAULT_MAX_FRAME_BYTES + DEFAULT_MAX_PENDING_BUFFER_SLACK_BYTES
    MAX_PENDING_WRITE_BYTES = DEFAULT_MAX_FRAME_BYTES + DEFAULT_MAX_PENDING_BUFFER_SLACK_BYTES
    MAX_CLIENTS = DEFAULT_MAX_CLIENTS
    CLIENT_IDLE_SECONDS = DEFAULT_CLIENT_IDLE_SECONDS
    DIALOG_TIMER_MS = DEFAULT_DIALOG_TIMER_MS
    AUTH_TOKEN = ""
    ALLOW_INSECURE_NO_AUTH = False

STOP_DIR = os.environ.get("VW_MCP_STOP_DIR") or os.path.join(
    os.path.expanduser("~"), ".vectorworks-mcp"
)
STOP_FILE = os.path.join(STOP_DIR, "STOP")
SCREENSHOT_DIR = STOP_DIR
_SHOULD_STOP = False
_DISPATCH_MODE = None
_STATE_MODULE = "_vw_mcp_listener_state"
_STATE = sys.modules.get(_STATE_MODULE)
if _STATE is None:
    _STATE = types.SimpleNamespace(
        listener_thread=None,
        listener_server=None,
        dialog_running=False,
        win_timer_id=None,
        win_timer_callback=None,
        win_timer_server=None,
        win_timer_user32=None,
        win_timer_busy=False,
    )
    sys.modules[_STATE_MODULE] = _STATE
else:
    if not hasattr(_STATE, "listener_thread"):
        _STATE.listener_thread = None
    if not hasattr(_STATE, "listener_server"):
        _STATE.listener_server = None
    if not hasattr(_STATE, "dialog_running"):
        _STATE.dialog_running = False
    if not hasattr(_STATE, "win_timer_id"):
        _STATE.win_timer_id = None
    if not hasattr(_STATE, "win_timer_callback"):
        _STATE.win_timer_callback = None
    if not hasattr(_STATE, "win_timer_server"):
        _STATE.win_timer_server = None
    if not hasattr(_STATE, "win_timer_user32"):
        _STATE.win_timer_user32 = None
    if not hasattr(_STATE, "win_timer_busy"):
        _STATE.win_timer_busy = False

# === HANDLE REGISTRY ===
_handles, _hcount = {}, [0]

def _reg(h):
    if h is None: return None
    _hcount[0] += 1
    hid = f"h{_hcount[0]}"
    _handles[hid] = h
    return hid

def _get(hid):
    return _handles.get(hid)

# === TYPE MAP ===
TYPE_NAMES = {
    2: "line", 3: "rect", 4: "oval", 5: "polygon", 6: "arc", 8: "freehand",
    10: "text", 11: "group", 13: "symbol", 15: "dimension", 16: "3d_polygon",
    18: "locus", 21: "extrude", 24: "mesh", 34: "wall", 38: "roof", 40: "floor",
    63: "roof_face", 68: "nurbs_curve", 71: "viewport", 85: "slab",
}

MAX_OBJECT_QUERY_LIMIT = 1000
MAX_WORKSHEET_ROWS = 500
MAX_POINT_LIST_LENGTH = 1000
_EXACT_NAME_CRITERIA_RE = re.compile(r"^\(\(N='[^']{1,255}'\)\)$")


def _is_exact_name_criteria(criteria):
    return bool(_EXACT_NAME_CRITERIA_RE.match(str(criteria or "").strip()))


def _bounded_int(value, default, min_value, max_value, name):
    if value is None or value == "":
        value = default
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise ValueError("{name} must be an integer".format(name=name))
    if value < min_value:
        raise ValueError("{name} must be >= {min}".format(name=name, min=min_value))
    if value > max_value:
        return max_value
    return value


def _point_pairs(value, name="points", min_points=0, max_points=MAX_POINT_LIST_LENGTH):
    if value is None:
        value = []
    if not isinstance(value, (list, tuple)):
        raise ValueError("{name} must be a list of [x, y] points".format(name=name))
    if len(value) < min_points:
        raise ValueError("{name} requires at least {count} points".format(name=name, count=min_points))
    if len(value) > max_points:
        raise ValueError("{name} cannot contain more than {count} points".format(name=name, count=max_points))
    points = []
    for index, point in enumerate(value):
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            raise ValueError("{name}[{index}] must be [x, y]".format(name=name, index=index))
        try:
            points.append((float(point[0]), float(point[1])))
        except (TypeError, ValueError):
            raise ValueError("{name}[{index}] must contain numeric x/y values".format(name=name, index=index))
    return points


def _obj_info(h):
    try:
        tn = vs.GetTypeN(h) if hasattr(vs, 'GetTypeN') else vs.GetType(h)
        bbox = vs.GetBBox(h)
        return {"handle": _reg(h), "type": TYPE_NAMES.get(tn, f"type_{tn}"), "type_id": tn,
                "name": vs.GetName(h) or "",
                "bounds": {"top_left": list(bbox[0]), "bottom_right": list(bbox[1])}}
    except Exception:
        return {"handle": _reg(h), "type": "unknown", "name": "", "bounds": None}

def _ok(result): return {"success": True, "result": result}
def _err(msg): return {"success": False, "error": msg}


class ProtocolError(Exception):
    pass


def _alert(message):
    if vs is not None and hasattr(vs, "AlrtDialog"):
        try:
            vs.AlrtDialog(message)
            return
        except Exception:
            pass
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass


def _message(message):
    if vs is not None and hasattr(vs, "Message"):
        try:
            vs.Message(message)
            return
        except Exception:
            pass
    _alert(message)


def _json_safe(value, depth=0):
    if depth > 25:
        return str(value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {str(k): _json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v, depth + 1) for v in value]
    return str(value)


def _response_bytes(response):
    try:
        return json.dumps(response, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")
    except Exception:
        safe = _json_safe(response)
        return json.dumps(safe, ensure_ascii=False, allow_nan=False).encode("utf-8")

def _set_rfield(h, rec, field, val):
    """Try SetRField and return an error string on failure."""
    try:
        vs.SetRField(h, rec, field, str(val))
        return ""
    except Exception as exc:
        return "{rec}.{field}: {err}".format(rec=rec, field=field, err=exc)

def _rgb_to_idx(r, g, b):
    """Convert RGB (0-65535) to VW color index."""
    if hasattr(vs, 'RGBToColorIndexN'):
        return vs.RGBToColorIndexN(r, g, b, False)
    if hasattr(vs, 'RGBToColorIndex'):
        return vs.RGBToColorIndex(r, g, b)
    return 0

def _set_object_rgb_color(h, prop, value):
    r, g, b = [int(x.strip()) for x in value.split(",")]
    for component_name, component in (("r", r), ("g", g), ("b", b)):
        if component < 0 or component > 65535:
            raise ValueError("{0} color component must be in 0..65535".format(component_name))
    if prop == "fillColor":
        if hasattr(vs, "SetFillFore"):
            vs.SetFillFore(h, (r, g, b))
        else:
            vs.SetFillForeColor(h, _rgb_to_idx(r, g, b))
    else:
        if hasattr(vs, "SetPenFore"):
            vs.SetPenFore(h, (r, g, b))
        else:
            vs.SetPenForeColor(h, _rgb_to_idx(r, g, b))

def _finite_float(value, default, label, min_value=None, max_value=None):
    if value is None:
        value = default
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError("{0} must be a finite number".format(label))
    if not math.isfinite(number):
        raise ValueError("{0} must be a finite number".format(label))
    if min_value is not None and number < min_value:
        raise ValueError("{0} must be >= {1}".format(label, min_value))
    if max_value is not None and number > max_value:
        raise ValueError("{0} must be <= {1}".format(label, max_value))
    return number

def _bounded_text(value, label, max_len=4096):
    if value is None:
        text = ""
    else:
        text = str(value)
    if not text:
        raise ValueError("{0} is required".format(label))
    if len(text) > max_len:
        raise ValueError("{0} must be at most {1} characters".format(label, max_len))
    return text

def _strict_bounded_int(value, default, label, min_value, max_value):
    number = _finite_float(value, default, label, min_value=min_value, max_value=max_value)
    if int(number) != number:
        raise ValueError("{0} must be an integer".format(label))
    return int(number)

def _bool_param(value, default=False):
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError("boolean value expected")

# === HANDLERS ===

def handle_run_script(p):
    if p.get("confirm") != "RUN_TRUSTED_CODE":
        return _err("run_script requires confirm='RUN_TRUSTED_CODE'")
    old = sys.stdout
    sys.stdout = cap = io.StringIO()
    try:
        exec(p.get("code", ""), {"vs": vs, "__builtins__": __builtins__})
        return _ok(cap.getvalue() or "OK")
    except Exception: return _err(traceback.format_exc())
    finally: sys.stdout = old

def handle_create_object(p):
    t = p.get("object_type", "").lower()
    x1, y1, x2, y2 = p.get("x1", 0), p.get("y1", 0), p.get("x2", 100), p.get("y2", 100)
    try:
        if t in ("rect", "rectangle", "box"): vs.Rect(x1, y1, x2, y2); t = "rect"
        elif t == "circle": vs.ArcByCenter(x1, y1, p.get("radius", 50), 0, 360)
        elif t == "oval": vs.Oval(x1, y1, x2, y2)
        elif t == "line": vs.MoveTo(x1, y1); vs.LineTo(x2, y2)
        elif t == "arc": vs.ArcByCenter(x1, y1, p.get("radius", 50), p.get("start_angle", 0), p.get("sweep_angle", 90))
        elif t == "polygon":
            pts = _point_pairs(p.get("points", []), min_points=2)
            (vs.ClosePoly if p.get("closed", True) else vs.OpenPoly)()
            vs.BeginPoly()
            for x, y in pts: vs.AddPoint(x, y)
            vs.EndPoly()
        else: return _err(f"Unknown type: {t}. Use: rect, circle, oval, line, arc, polygon")
        h = vs.LNewObj()
        if h and p.get("name"): vs.SetName(h, p["name"])
        if h and p.get("class_name"): vs.SetClass(h, p["class_name"])
        vs.ReDrawAll()
        return _ok(f"Created {t}, handle: {_reg(h)}")
    except Exception: return _err(traceback.format_exc())

def _reject_json_constant(value):
    raise ValueError("non-finite JSON constants are not allowed: {0}".format(value))

def _reject_duplicate_object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: {0}".format(key))
        result[key] = value
    return result

def _json_loads_strict(text):
    return json.loads(
        text,
        parse_constant=_reject_json_constant,
        object_pairs_hook=_reject_duplicate_object_pairs,
    )

def _created_handle_from_result(result):
    if isinstance(result, str) and "handle:" in result:
        return result.split("handle:", 1)[1].strip().split()[0].strip(",;")
    return ""

def handle_batch_create_objects(p):
    try:
        count = _bounded_int(p.get("object_count", 0), 0, 1, 250, "object_count")
    except ValueError as e:
        return _err(str(e))
    created = []
    for index in range(1, count + 1):
        key = "object_{0}_json".format(index)
        raw = p.get(key, "")
        if not isinstance(raw, str) or not raw:
            return _err("{0} is required".format(key))
        try:
            item = _json_loads_strict(raw)
        except Exception as e:
            return _err("{0} must be valid JSON object: {1}".format(key, e))
        if not isinstance(item, dict):
            return _err("{0} must be a JSON object".format(key))
        result = handle_create_object(item)
        if result.get("success") is not True:
            return _err(
                "non-atomic batch_create_objects failed at index {0}: {1}. Earlier primitives may already exist.".format(
                    index,
                    result.get("error", "unknown error"),
                )
            )
        object_type = str(item.get("object_type") or item.get("type") or "rect").lower()
        if object_type in ("rectangle", "box"):
            object_type = "rect"
        created.append({"index": index, "type": object_type, "handle": _created_handle_from_result(result.get("result"))})
    return _ok({"atomic": False, "rollback_on_error": False, "created_count": len(created), "created": created})

def handle_get_layers(p):
    layers, h = [], vs.FLayer()
    while h is not None:
        try: layers.append({"name": vs.GetLName(h), "visible": vs.GetLVis(h) == 0})
        except Exception: pass
        h = vs.NextLayer(h)
    return _ok(layers)

def handle_get_objects(p):
    try:
        limit = _bounded_int(p.get("limit", 100), 100, 1, MAX_OBJECT_QUERY_LIMIT, "limit")
    except ValueError as e:
        return _err(str(e))
    target_layer, target_type = p.get("layer", ""), p.get("object_type", "").lower()
    objs = []
    def collect(lh):
        obj = vs.FInLayer(lh)
        while obj is not None and len(objs) < limit:
            info = _obj_info(obj)
            if not target_type or info["type"] == target_type: objs.append(info)
            obj = vs.NextObj(obj)
    if target_layer:
        lh = vs.GetObject(target_layer)
        if lh is None: return _err(f"Layer '{target_layer}' not found")
        collect(lh)
    else:
        lh = vs.FLayer()
        while lh is not None and len(objs) < limit:
            collect(lh); lh = vs.NextLayer(lh)
    return _ok(objs)

def handle_set_property(p):
    h = _get(p.get("handle", ""))
    if h is None: return _err(f"Handle '{p.get('handle')}' not found. Use vw_get_objects first.")
    prop, val = p.get("property_name", ""), p.get("value", "")
    try:
        if prop == "name": vs.SetName(h, val)
        elif prop == "class": vs.SetClass(h, val)
        elif prop in ("fillColor", "penColor"):
            _set_object_rgb_color(h, prop, val)
        elif prop == "lineWeight": vs.SetLW(h, int(val))
        elif prop == "opacity": vs.SetOpacity(h, int(val))
        else: return _err(f"Unknown property: {prop}. Use: name, class, fillColor, penColor, lineWeight, opacity")
        vs.ReDrawAll()
        return _ok(f"Set {prop}={val}")
    except Exception: return _err(traceback.format_exc())

def handle_find_objects(p):
    try:
        limit = _bounded_int(p.get("limit", 100), 100, 1, MAX_OBJECT_QUERY_LIMIT, "limit")
    except ValueError as e:
        return _err(str(e))
    results = []
    def collect(h):
        if len(results) < limit: results.append(_obj_info(h))
    try:
        vs.ForEachObject(collect, p.get("criteria", "ALL"))
        return _ok(results)
    except Exception: return _err(traceback.format_exc())

def handle_manage_classes(p):
    action = p.get("action", "list").lower()
    try:
        if action == "list":
            return _ok([vs.ClassList(i) for i in range(1, vs.ClassNum() + 1)])
        elif action == "create":
            vs.NameClass(p.get("class_name", "")); return _ok(f"Created class '{p.get('class_name')}'")
        elif action == "delete":
            if p.get("confirm") != "DELETE_CLASS":
                return _err("class deletion requires confirm='DELETE_CLASS'")
            vs.DelClass(p.get("class_name", "")); return _ok(f"Deleted class '{p.get('class_name')}'")
        else: return _err("Unknown action. Use: list, create, delete")
    except Exception: return _err(traceback.format_exc())

def handle_worksheet(p):
    action, ws_name = p.get("action", "list").lower(), p.get("worksheet_name", "")
    try:
        row = _bounded_int(p.get("row", 1), 1, 1, 1048576, "row")
        col = _bounded_int(p.get("col", 1), 1, 1, 16384, "col")
        num_rows = _bounded_int(p.get("num_rows", 10), 10, 1, MAX_WORKSHEET_ROWS, "num_rows")
        if action == "list":
            ws = []
            vs.ForEachObject(lambda h: ws.append(vs.GetName(h)), "T=WORKSHEET")
            return _ok(ws)
        ws_h = vs.GetObject(ws_name)
        if ws_h is None: return _err(f"Worksheet '{ws_name}' not found")
        if action == "read":
            if hasattr(vs, "GetWSCellString"):
                value = vs.GetWSCellString(ws_h, row, col)
            else:
                value = vs.GetWSCellValue(ws_h, row, col)
            return _ok({"row": row, "col": col, "value": str(value)})
        elif action == "read_range":
            data = []
            for r in range(row, row + num_rows):
                rd = []
                for c in range(col, col + 20):
                    v = vs.GetWSCellString(ws_h, r, c) if hasattr(vs, "GetWSCellString") else vs.GetWSCellValue(ws_h, r, c)
                    if v is None or str(v).strip() == "": break
                    rd.append(str(v))
                if not rd: break
                data.append(rd)
            return _ok(data)
        elif action == "write":
            if not hasattr(vs, "SetWSCellFormula"):
                return _err("Vectorworks API SetWSCellFormula is not available; cannot write worksheet cells safely")
            vs.SetWSCellFormula(ws_h, row, col, row, col, p.get("value", "")); vs.ReDrawAll()
            return _ok(f"Set cell ({row},{col})")
        else: return _err("Unknown action. Use: list, read, read_range, write")
    except Exception: return _err(traceback.format_exc())

def handle_symbol(p):
    action = p.get("action", "list").lower()
    try:
        if action == "list":
            syms = set()
            h = vs.FSymDef()
            while h is not None:
                syms.add(vs.GetName(h)); h = vs.NextSymDef(h)
            return _ok(list(syms))
        elif action == "insert":
            vs.Symbol(p.get("symbol_name", ""), p.get("x", 0), p.get("y", 0), p.get("rotation", 0))
            h = vs.LNewObj(); vs.ReDrawAll()
            return _ok(f"Inserted symbol, handle: {_reg(h)}")
        else: return _err("Unknown action. Use: list, insert")
    except Exception: return _err(traceback.format_exc())

def handle_export(p):
    fmt, fp = p.get("format", "").lower(), p.get("file_path", "")
    if not fp: return _err("file_path is required")
    menu = {"pdf": "Export PDF", "dxf": "Export DXF/DWG", "dwg": "Export DXF/DWG", "image": "Export Image File"}
    if fmt not in menu: return _err(f"Unknown format: {fmt}. Use: pdf, dxf, dwg, image")
    try:
        vs.DoMenuTextByName(menu[fmt], 0)
        return _ok({
            "dialog_opened": True,
            "format": fmt,
            "requested_path": fp,
            "saved": False,
            "requires_user_save": True,
            "message": "{fmt} export dialog opened; choose the requested path in Vectorworks to save.".format(fmt=fmt.upper()),
        })
    except Exception: return _err(traceback.format_exc())

def handle_import_file(p):
    fp, fmt = p.get("file_path", ""), p.get("format", "auto").lower()
    if not fp: return _err("file_path is required")
    if not os.path.exists(fp): return _err(f"File not found: {fp}")
    if fmt == "auto": fmt = os.path.splitext(fp)[1].lstrip(".").lower()
    try:
        if fmt in ("dxf", "dwg"):
            result = vs.ImportDXFDWGFile(fp); vs.ReDrawAll()
            return _ok({"format": fmt, "file_path": fp, "import_result": result})
        elif fmt in ("png", "jpg", "jpeg", "tif", "tiff", "bmp"):
            h = vs.ImportImageFile(fp, (0, 0))
            if h is None and hasattr(vs, "LNewObj"):
                h = vs.LNewObj()
            vs.ReDrawAll()
            return _ok(f"Imported image: {fp}, handle: {_reg(h)}")
        else: return _err(f"Unsupported format: {fmt}. Use: dxf, dwg, png, jpg")
    except Exception: return _err(traceback.format_exc())

def handle_get_document_info(p):
    try:
        info = {"filename": vs.GetFName() or "Untitled", "filepath": vs.GetFPathName() or "",
                "layers": [], "layer_count": 0, "total_objects": 0}
        lh = vs.FLayer()
        while lh is not None:
            info["layer_count"] += 1
            info["layers"].append(vs.GetLName(lh))
            oh = vs.FInLayer(lh)
            while oh is not None: info["total_objects"] += 1; oh = vs.NextObj(oh)
            lh = vs.NextLayer(lh)
        return _ok(info)
    except Exception: return _err(traceback.format_exc())

def handle_screenshot(p):
    fp = p.get("file_path", "") or os.path.join(SCREENSHOT_DIR, "screenshot.png")
    try:
        os.makedirs(os.path.dirname(fp), exist_ok=True)
    except OSError:
        pass
    try:
        vs.DoMenuTextByName("Export Image File", 0)
        return _ok({
            "dialog_opened": True,
            "requested_path": fp,
            "saved": False,
            "requires_user_save": True,
            "message": "Export Image File dialog opened; choose the requested path in Vectorworks to save.",
        })
    except Exception: return _err(traceback.format_exc())


def handle_stop(p):
    global _SHOULD_STOP
    _SHOULD_STOP = True
    return _ok("Listener stop requested")

def _iter_selected():
    """Iterate selected objects using FSObject/NextSObj."""
    h = vs.FSObject() if hasattr(vs, 'FSObject') else vs.FSActLayer()
    while h is not None:
        yield h
        h = vs.NextSObj(h)

def handle_selection(p):
    action, criteria = p.get("action", "get").lower(), p.get("criteria", "")
    try:
        if action == "get":
            return _ok([_obj_info(h) for i, h in enumerate(_iter_selected()) if i < 200])
        elif action == "select":
            if not criteria: return _err("criteria required")
            vs.DSelectAll(); vs.SelectObj(criteria); vs.ReDrawAll()
            return _ok(f"Selected {sum(1 for _ in _iter_selected())} objects")
        elif action == "clear":
            vs.DSelectAll(); vs.ReDrawAll(); return _ok("Selection cleared")
        elif action == "delete":
            if criteria:
                if not _is_exact_name_criteria(criteria):
                    return _err("selection delete criteria must be exact object-name criteria like ((N='Name'))")
                if p.get("confirm") != "DELETE_EXACT_NAME":
                    return _err("selection delete with criteria requires confirm='DELETE_EXACT_NAME'")
            elif p.get("confirm") != "DELETE_SELECTED":
                return _err("selection delete requires confirm='DELETE_SELECTED'")
            if criteria:
                vs.DSelectAll()
                vs.SelectObj(criteria)
            to_del = list(_iter_selected())
            for h in to_del: vs.DelObject(h)
            vs.ReDrawAll(); return _ok(f"Deleted {len(to_del)} objects")
        elif action == "move":
            if not criteria: return _err("Provide dx,dy (e.g. '100,50')")
            dx, dy = [float(x) for x in criteria.split(",")]
            count = 0
            for h in _iter_selected(): vs.HMove(h, dx, dy); count += 1
            vs.ReDrawAll(); return _ok(f"Moved {count} objects by ({dx},{dy})")
        elif action == "duplicate":
            handles = []
            for h in _iter_selected():
                nh = vs.HDuplicate(h, 10, 10)
                handles.append(_reg(nh))
            vs.ReDrawAll(); return _ok(f"Duplicated {len(handles)} objects: {handles}")
        else: return _err("Unknown action. Use: get, select, clear, delete, move, duplicate")
    except Exception: return _err(traceback.format_exc())

# === PARAMETRIC ARCHITECTURAL HANDLERS ===

def handle_create_wall(p):
    sx, sy, ex, ey = p.get("start_x", 0), p.get("start_y", 0), p.get("end_x", 1000), p.get("end_y", 0)
    height, thickness = p.get("height", 3000), p.get("thickness", 200)
    try:
        vs.Wall(sx, sy, ex, ey)
        h = vs.LNewObj()
        if h is None: return _err("Failed to create wall")
        field_errors = [
            err for err in (
                _set_rfield(h, 'Wall', 'Height', height),
                _set_rfield(h, 'Wall', 'Thickness', thickness),
                _set_rfield(h, 'Wall', 'Width', thickness),
                _set_rfield(h, 'Wall', 'Style', p["style_name"]) if p.get("style_name") else "",
            ) if err
        ]
        vs.ReDrawAll()
        result = {"message": f"Created wall ({sx},{sy})->({ex},{ey}), h={height}, t={thickness}", "handle": _reg(h)}
        if field_errors:
            result["field_warnings"] = field_errors
        return _ok(result)
    except Exception: return _err(traceback.format_exc())

def handle_create_text(p):
    try:
        text = _bounded_text(p.get("text", ""), "text")
        x1 = _finite_float(p.get("x1", p.get("x", 0)), 0, "x1")
        y1 = _finite_float(p.get("y1", p.get("y", 0)), 0, "y1")
        width = _finite_float(p.get("width", 0), 0, "width", min_value=0)
        rotation = _finite_float(p.get("rotation", 0), 0, "rotation")
        text_size = _finite_float(p.get("text_size", p.get("size", 0)), 0, "text_size", min_value=0)
        fixed_size = _bool_param(p.get("fixed_size"), False)
        wrap = _bool_param(p.get("wrap"), width > 0)
        warnings = []
        h = None
        if hasattr(vs, "CreateTextBlock"):
            h = vs.CreateTextBlock(text, (x1, y1), fixed_size, width)
        else:
            if fixed_size:
                warnings.append("fixed_size requires Vectorworks CreateTextBlock; created regular text instead")
            if hasattr(vs, "TextOrigin"): vs.TextOrigin(x1, y1)
        if h is None:
            if hasattr(vs, "CreateText"):
                vs.CreateText(text)
            elif hasattr(vs, "Text"):
                vs.Text(text)
            else:
                return _err("Vectorworks API CreateText/Text is not available")
        if h is None:
            h = vs.LNewObj()
        if h and p.get("name"): vs.SetName(h, p["name"])
        if h and p.get("class_name"): vs.SetClass(h, p["class_name"])
        if h and width and hasattr(vs, "SetTextWidth"): vs.SetTextWidth(h, width)
        elif h and width:
            warnings.append("SetTextWidth is not available in this Vectorworks Python API")
        if h and hasattr(vs, "SetTextWrap"): vs.SetTextWrap(h, bool(wrap or width > 0))
        elif h and wrap:
            warnings.append("SetTextWrap is not available in this Vectorworks Python API")
        if h and text_size > 0 and hasattr(vs, "SetTextSize"):
            char_size = vs.PagePointsToCoordLength(text_size) if hasattr(vs, "PagePointsToCoordLength") else text_size
            vs.SetTextSize(h, 0, len(text), char_size)
        elif h and text_size > 0:
            warnings.append("SetTextSize is not available in this Vectorworks Python API")
        if h and rotation and hasattr(vs, "HRotate"): vs.HRotate(h, x1, y1, rotation)
        vs.ReDrawAll()
        result = {"type": "text", "handle": _reg(h)}
        if warnings:
            result["field_warnings"] = warnings
        return _ok(result)
    except Exception: return _err(traceback.format_exc())

def handle_create_linear_dimension(p):
    try:
        sx = _finite_float(p.get("start_x", p.get("x1", 0)), 0, "start_x")
        sy = _finite_float(p.get("start_y", p.get("y1", 0)), 0, "start_y")
        ex = _finite_float(p.get("end_x", p.get("x2", 1000)), 1000, "end_x")
        ey = _finite_float(p.get("end_y", p.get("y2", 0)), 0, "end_y")
        offset = _finite_float(p.get("offset", p.get("dimension_offset", 300)), 300, "offset")
        text_offset = _finite_float(p.get("text_offset", 0), 0, "text_offset")
        direction_x = _finite_float(p.get("direction_x", 0), 0, "direction_x")
        direction_y = _finite_float(p.get("direction_y", 0), 0, "direction_y")
        dimension_type = _strict_bounded_int(p.get("dimension_type", 1), 1, "dimension_type", 0, 2)
        if sx == ex and sy == ey: return _err("linear_dimension endpoints must not be identical")
        if hasattr(vs, "LinearDim"):
            vs.LinearDim(sx, sy, ex, ey, offset, dimension_type)
        elif hasattr(vs, "CreateLinearDimension"):
            vs.CreateLinearDimension((sx, sy), (ex, ey), offset, text_offset, (direction_x, direction_y), dimension_type)
        else:
            return _err("Vectorworks API LinearDim/CreateLinearDimension is not available")
        h = vs.LNewObj()
        if h and p.get("name"): vs.SetName(h, p["name"])
        if h and p.get("class_name"): vs.SetClass(h, p["class_name"])
        vs.ReDrawAll()
        return _ok({"type": "linear_dimension", "handle": _reg(h)})
    except Exception: return _err(traceback.format_exc())

def handle_insert_door(p):
    try:
        h = vs.CreateCustomObjectN('Door', (p.get("x", 0), p.get("y", 0)), p.get("rotation", 0), False)
        if h is None: return _err("Failed to create Door. Is the plugin available?")
        field_errors = [
            err for err in (
                _set_rfield(h, 'Door', 'Width', p.get("width", 900)),
                _set_rfield(h, 'Door', 'Height', p.get("height", 2100)),
            ) if err
        ]
        vs.ReDrawAll()
        result = {"message": f"Inserted door {p.get('width',900)}x{p.get('height',2100)}", "handle": _reg(h)}
        if field_errors:
            result["field_warnings"] = field_errors
        return _ok(result)
    except Exception: return _err(traceback.format_exc())

def handle_insert_window(p):
    try:
        h = vs.CreateCustomObjectN('Window', (p.get("x", 0), p.get("y", 0)), p.get("rotation", 0), False)
        if h is None: return _err("Failed to create Window. Is the plugin available?")
        field_errors = [
            err for err in (
                _set_rfield(h, 'Window', 'Width', p.get("width", 1200)),
                _set_rfield(h, 'Window', 'Height', p.get("height", 1500)),
                _set_rfield(h, 'Window', 'Elevation In Wall', p.get("sill_height", 900)),
                _set_rfield(h, 'Window', 'SillHeight', p.get("sill_height", 900)),
            ) if err
        ]
        vs.ReDrawAll()
        result = {"message": "Inserted window", "handle": _reg(h)}
        if field_errors:
            result["field_warnings"] = field_errors
        return _ok(result)
    except Exception: return _err(traceback.format_exc())

def handle_create_slab(p):
    try:
        pts = _point_pairs(p.get("points", []), min_points=3)
    except ValueError as e:
        return _err(str(e))
    thickness, elev = p.get("thickness", 200), p.get("elevation", 0)
    try:
        vs.BeginXtrd(elev, elev + thickness)
        vs.ClosePoly(); vs.BeginPoly()
        for x, y in pts: vs.AddPoint(x, y)
        vs.EndPoly(); vs.EndXtrd()
        h = vs.LNewObj(); vs.ReDrawAll()
        return _ok(f"Created slab, {len(pts)} pts, t={thickness}, handle: {_reg(h)}")
    except Exception: return _err(traceback.format_exc())

def handle_create_roof(p):
    try:
        pts = _point_pairs(p.get("points", []), min_points=3)
    except ValueError as e:
        return _err(str(e))
    bh, slope, oh, thick = p.get("bearing_height", 3000), p.get("slope", 30), p.get("overhang", 500), p.get("thickness", 200)
    try:
        cx, cy = sum(x[0] for x in pts)/len(pts), sum(x[1] for x in pts)/len(pts)
        h = vs.CreateCustomObjectN('Roof', (cx, cy), 0, False)
        if h is not None:
            field_errors = []
            for f, v in [('Slope', slope), ('Bearing Height', bh), ('Overhang', oh), ('Thickness', thick)]:
                err = _set_rfield(h, 'Roof', f, v)
                if err:
                    field_errors.append(err)
            vs.ReDrawAll()
            result = {"message": f"Created roof, slope={slope}deg", "handle": _reg(h)}
            if field_errors:
                result["field_warnings"] = field_errors
            return _ok(result)
        # Fallback: flat extrusion
        vs.BeginXtrd(bh, bh + thick)
        vs.ClosePoly(); vs.BeginPoly()
        for x, y in pts: vs.AddPoint(x, y)
        vs.EndPoly(); vs.EndXtrd()
        vs.ReDrawAll()
        return _ok(f"Created flat roof at z={bh}, handle: {_reg(vs.LNewObj())}")
    except Exception: return _err(traceback.format_exc())

def handle_inspect_object(p):
    hid, pname = p.get("handle", ""), p.get("plugin_name", "")
    try:
        h, temp = None, False
        if hid:
            h = _get(hid)
            if h is None: return _err(f"Handle '{hid}' not found")
        elif pname:
            if p.get("confirm") != "PROBE_PLUGIN":
                return _err("plugin probing requires confirm='PROBE_PLUGIN'")
            h = vs.CreateCustomObjectN(pname, (0, 0), 0, False)
            if h is None: return _err(f"Cannot create '{pname}'. Check plugin name.")
            temp = True
        else: return _err("Provide handle or plugin_name")

        tn = vs.GetTypeN(h) if hasattr(vs, 'GetTypeN') else vs.GetType(h)
        info = {"type": TYPE_NAMES.get(tn, f"type_{tn}"), "type_id": tn,
                "name": vs.GetName(h) or "", "fields": []}
        # Enumerate parametric record fields
        try:
            rec = vs.GetParametricRecord(h)
            if rec:
                rec_name = pname or info["name"]
                for i in range(1, vs.NumFields(rec) + 1):
                    fname = vs.GetFldName(rec, i)
                    try: fval = str(vs.GetRField(h, rec_name, fname))
                    except Exception: fval = ""
                    info["fields"].append({"name": fname, "value": fval})
        except Exception as e:
            info["fields_error"] = str(e)
        if temp: vs.DelObject(h); vs.ReDrawAll()
        return _ok(info)
    except Exception: return _err(traceback.format_exc())

# === DISPATCHER ===
HANDLERS = {
    "run_script": handle_run_script, "create_object": handle_create_object,
    "batch_create_objects": handle_batch_create_objects,
    "get_layers": handle_get_layers, "get_objects": handle_get_objects,
    "set_property": handle_set_property, "find_objects": handle_find_objects,
    "manage_classes": handle_manage_classes, "worksheet": handle_worksheet,
    "symbol": handle_symbol, "export": handle_export,
    "import_file": handle_import_file, "get_document_info": handle_get_document_info,
    "screenshot": handle_screenshot, "stop": handle_stop, "selection": handle_selection,
    "create_wall": handle_create_wall, "insert_door": handle_insert_door,
    "create_text": handle_create_text, "create_linear_dimension": handle_create_linear_dimension,
    "insert_window": handle_insert_window, "create_slab": handle_create_slab,
    "create_roof": handle_create_roof, "inspect_object": handle_inspect_object,
}


def _bridge_status():
    mode = _DISPATCH_MODE or "unknown"
    transport_only = mode in ("background", "win_timer", "foreground")
    cad_api_safe = mode == "dialog"
    if mode == "dialog":
        bridge_kind = "python_dialog_agent_session"
    elif mode == "foreground":
        bridge_kind = "python_foreground_diagnostic"
    elif transport_only:
        bridge_kind = "python_transport_only"
    else:
        bridge_kind = "python_unknown"

    return {
        "pong": True,
        "handlers": len(HANDLERS),
        "version": __VERSION__,
        "bridge_kind": bridge_kind,
        "dispatch_mode": mode,
        "cad_api_safe": cad_api_safe,
        "transport_only": transport_only,
        "native_bridge": False,
    }


HANDLERS["ping"] = lambda p: _ok(_bridge_status())

def _request_id(req):
    if not isinstance(req, dict):
        return ""
    rid = req.get("id", "")
    return rid if isinstance(rid, str) else ""


def _request_error(req, message):
    return {"id": _request_id(req), "success": False, "error": message}


def dispatch(req):
    if not isinstance(req, dict):
        return _request_error(req, "Request must be a JSON object")
    if not isinstance(req.get("id"), str) or not req.get("id"):
        return _request_error(req, "Request id must be a non-empty string")
    auth_error = _auth_required_error()
    if auth_error:
        return _request_error(req, auth_error)
    if AUTH_TOKEN and req.get("auth_token") != AUTH_TOKEN:
        return _request_error(req, "Listener authentication failed")
    action = req.get("action", "")
    if not isinstance(action, str) or not action:
        return _request_error(req, "Request action must be a non-empty string")
    params = req.get("params", {}) if "params" in req else {}
    if not isinstance(params, dict):
        return _request_error(req, "Request params must be a JSON object")
    if _DISPATCH_MODE in ("background", "win_timer", "foreground") and action not in ("ping", "stop"):
        return {
            "id": _request_id(req),
            "success": False,
            "error": (
                "VW_MCP_MODE={mode} is transport-only. It can answer ping/stop, "
                "but Vectorworks API calls deadlock outside a normal Vectorworks "
                "script context. Use dialog mode for temporary agent-controlled "
                "CAD operations, or build the native Vectorworks SDK bridge for "
                "non-modal long-running control."
            ).format(mode=_DISPATCH_MODE),
        }
    handler = HANDLERS.get(action)
    if not handler:
        return _request_error(req, f"Unknown action: {action}")
    try:
        result = handler(params)
    except Exception:
        result = {"success": False, "error": traceback.format_exc()}
    result["id"] = _request_id(req)
    return result


# === NON-BLOCKING SOCKET LAYER ===
# Single event loop (selectors). Each connection buffers bytes until a full
# length-prefixed JSON frame is available; we decode and dispatch on this
# listener thread, then queue the response.

class _ClientState:
    __slots__ = (
        "rbuf",
        "wbuf",
        "need",
        "max_frame_bytes",
        "max_pending_read_bytes",
        "max_pending_write_bytes",
        "last_activity",
    )
    def __init__(self, max_frame_bytes=None, max_pending_read_bytes=None, max_pending_write_bytes=None):
        self.rbuf = bytearray()
        self.wbuf = bytearray()
        self.need = None  # current frame body length, or None if waiting for header
        self.max_frame_bytes = max_frame_bytes or MAX_FRAME_BYTES
        self.max_pending_read_bytes = max_pending_read_bytes or MAX_PENDING_READ_BYTES
        self.max_pending_write_bytes = max_pending_write_bytes or MAX_PENDING_WRITE_BYTES
        self.last_activity = time.time()

    def touch(self):
        self.last_activity = time.time()

    def feed(self, chunk):
        if len(self.rbuf) + len(chunk) > self.max_pending_read_bytes:
            raise ProtocolError(
                "pending read buffer exceeds VW_MCP_MAX_PENDING_READ_BYTES={m}".format(
                    m=self.max_pending_read_bytes
                )
            )
        self.rbuf.extend(chunk)
        self.touch()

    def pop_frame(self):
        if self.need is None:
            if len(self.rbuf) < 4:
                return None
            self.need = struct.unpack(">I", bytes(self.rbuf[:4]))[0]
            del self.rbuf[:4]
            if self.need <= 0:
                raise ProtocolError("invalid frame length {n}".format(n=self.need))
            if self.need > self.max_frame_bytes:
                raise ProtocolError(
                    "frame length {n} exceeds VW_MCP_MAX_FRAME_BYTES={m}".format(
                        n=self.need, m=self.max_frame_bytes
                    )
                )
        if len(self.rbuf) < self.need:
            return None
        body = bytes(self.rbuf[: self.need])
        del self.rbuf[: self.need]
        self.need = None
        return body

    def enqueue(self, payload: bytes):
        if len(payload) > self.max_frame_bytes:
            payload = _response_bytes(_err("response exceeded max frame size"))
        frame = struct.pack(">I", len(payload)) + payload
        if len(self.wbuf) + len(frame) > self.max_pending_write_bytes:
            raise ProtocolError(
                "pending write buffer exceeds VW_MCP_MAX_PENDING_WRITE_BYTES={m}".format(
                    m=self.max_pending_write_bytes
                )
            )
        self.wbuf.extend(frame)


def _stop_requested():
    if _SHOULD_STOP:
        return True
    try:
        if os.path.exists(STOP_FILE):
            try: os.remove(STOP_FILE)
            except OSError: pass
            return True
    except Exception:
        pass
    return False


def _drop(sel, fileobj):
    try: sel.unregister(fileobj)
    except (KeyError, ValueError): pass
    try: fileobj.close()
    except OSError: pass


def _client_events(state):
    if state.wbuf:
        return selectors.EVENT_WRITE
    return selectors.EVENT_READ


def _set_client_events(sel, fileobj, state):
    try:
        sel.modify(fileobj, _client_events(state), data=state)
    except (KeyError, ValueError, OSError):
        pass


def _has_pending_writes(sel):
    try:
        for key in sel.get_map().values():
            state = key.data
            if state is not None and state.wbuf:
                return True
    except Exception:
        return False
    return False


def _client_count(sel):
    try:
        return sum(1 for key in sel.get_map().values() if key.data is not None)
    except Exception:
        return 0


def _drop_idle_clients(sel):
    if CLIENT_IDLE_SECONDS <= 0:
        return
    now = time.time()
    try:
        keys = list(sel.get_map().values())
    except Exception:
        return
    for key in keys:
        state = key.data
        if state is None:
            continue
        if now - state.last_activity > CLIENT_IDLE_SECONDS:
            _drop(sel, key.fileobj)


class _ListenerServer:
    def __init__(self, show_alerts=True):
        self.show_alerts = show_alerts
        self.server_sock = None
        self.sel = None
        self.closed = False

    def start(self):
        global _SHOULD_STOP, _DISPATCH_MODE
        _SHOULD_STOP = False
        if _DISPATCH_MODE is None:
            _DISPATCH_MODE = "foreground"

        if vs is None:
            if self.show_alerts:
                _alert("VW MCP Listener must be run inside Vectorworks, where the 'vs' module is available.")
            return False
        if _CONFIG_ERROR:
            if self.show_alerts:
                _alert("VW MCP configuration error: {e}".format(e=_CONFIG_ERROR))
            return False

        try:
            os.makedirs(STOP_DIR, exist_ok=True)
        except OSError as e:
            if self.show_alerts:
                _alert("VW MCP could not create stop directory:\n{d}\n{e}".format(d=STOP_DIR, e=e))
            return False
        _stop_requested()  # clear any stale STOP from a previous session

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server_sock.bind((HOST, PORT))
        except OSError as e:
            if self.show_alerts:
                _alert(
                    "VW MCP failed to bind {h}:{p}\n{e}\n\n"
                    "Is another listener already running? Close it or set VW_MCP_PORT.".format(
                        h=HOST, p=PORT, e=e
                    )
                )
            server_sock.close()
            return False
        server_sock.listen(MAX_CLIENTS)
        server_sock.setblocking(False)

        sel = selectors.DefaultSelector()
        sel.register(server_sock, selectors.EVENT_READ, data=None)

        self.server_sock = server_sock
        self.sel = sel
        self.closed = False
        _STATE.listener_server = self

        if self.show_alerts:
            _alert(
                "VW MCP Listener STARTED (socket)\n"
                "Listening on {h}:{p}\n"
                "Version {v}\n"
                "Stop: create STOP file in:\n{d}".format(h=HOST, p=PORT, v=__VERSION__, d=STOP_DIR)
            )
        return True

    def _handle_key(self, key, mask):
        fileobj = key.fileobj

        if key.data is None:
            try:
                conn, _addr = fileobj.accept()
            except BlockingIOError:
                return
            _drop_idle_clients(self.sel)
            if _client_count(self.sel) >= MAX_CLIENTS:
                try: conn.close()
                except OSError: pass
                return
            conn.setblocking(False)
            self.sel.register(
                conn,
                selectors.EVENT_READ,
                data=_ClientState(
                    max_frame_bytes=MAX_FRAME_BYTES,
                    max_pending_read_bytes=MAX_PENDING_READ_BYTES,
                    max_pending_write_bytes=MAX_PENDING_WRITE_BYTES,
                ),
            )
            return

        state = key.data

        if mask & selectors.EVENT_READ:
            try:
                chunk = fileobj.recv(65536)
            except (BlockingIOError, InterruptedError):
                pass
            except (ConnectionError, OSError):
                _drop(self.sel, fileobj)
                return
            else:
                if chunk == b"":
                    _drop(self.sel, fileobj)
                    return
                try:
                    state.feed(chunk)
                except ProtocolError:
                    _drop(self.sel, fileobj)
                    return
                while True:
                    try:
                        frame = state.pop_frame()
                    except ProtocolError:
                        _drop(self.sel, fileobj)
                        return
                    if frame is None:
                        break
                    rid = ""
                    try:
                        req = _json_loads_strict(frame.decode("utf-8"))
                        rid = req.get("id", "")
                        resp = dispatch(req)
                    except Exception as e:
                        resp = {"id": rid, "success": False, "error": "bad JSON: {}".format(e)}
                    try:
                        state.enqueue(_response_bytes(resp))
                    except ProtocolError:
                        _drop(self.sel, fileobj)
                        return
                    _set_client_events(self.sel, fileobj, state)
                    if state.wbuf:
                        break

        if mask & selectors.EVENT_WRITE and state.wbuf:
            try:
                sent = fileobj.send(state.wbuf)
                if sent == 0:
                    _drop(self.sel, fileobj)
                    return
                del state.wbuf[:sent]
                state.touch()
                _set_client_events(self.sel, fileobj, state)
            except (BlockingIOError, InterruptedError):
                pass
            except (ConnectionError, OSError):
                _drop(self.sel, fileobj)

    def pump_once(self, timeout=0.0):
        if self.closed or self.sel is None:
            return False
        _drop_idle_clients(self.sel)
        if _stop_requested() and not _has_pending_writes(self.sel):
            self.close()
            return False
        try:
            events = self.sel.select(timeout=timeout)
        except (OSError, ValueError):
            self.close()
            return False
        for key, mask in events:
            self._handle_key(key, mask)
        _drop_idle_clients(self.sel)
        if _stop_requested() and not _has_pending_writes(self.sel):
            self.close()
            return False
        return not self.closed

    def pump(self, iterations=8, timeout=0.0):
        for index in range(max(1, iterations)):
            if not self.pump_once(timeout if index == 0 else 0.0):
                return False
        return True

    def serve_forever(self):
        try:
            while self.pump_once(timeout=0.1):
                pass
        finally:
            self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        sel = self.sel
        server_sock = self.server_sock
        self.sel = None
        self.server_sock = None
        try:
            if sel is not None:
                for key in list(sel.get_map().values()):
                    try: key.fileobj.close()
                    except OSError: pass
        except Exception:
            pass
        try:
            if sel is not None:
                sel.close()
        except Exception:
            pass
        try:
            if server_sock is not None:
                server_sock.close()
        except OSError:
            pass
        if getattr(_STATE, "listener_server", None) is self:
            _STATE.listener_server = None
        global _DISPATCH_MODE
        if _DISPATCH_MODE in ("foreground", "dialog"):
            _DISPATCH_MODE = None
        if self.show_alerts:
            _alert("VW MCP Listener STOPPED.")


def main(show_alerts=True):
    server = _ListenerServer(show_alerts=show_alerts)
    if server.start():
        server.serve_forever()


def _listener_port_open():
    try:
        sock = socket.create_connection((HOST, PORT), timeout=0.2)
    except OSError:
        return False
    try:
        sock.close()
    except OSError:
        pass
    return True


def _existing_listener_status():
    request = {"id": "startup-ping", "action": "ping", "params": {}}
    payload = json.dumps(request).encode("utf-8")
    try:
        sock = socket.create_connection((HOST, PORT), timeout=0.6)
        sock.settimeout(0.6)
        try:
            sock.sendall(struct.pack(">I", len(payload)) + payload)
            header = b""
            while len(header) < 4:
                chunk = sock.recv(4 - len(header))
                if not chunk:
                    return None
                header += chunk
            size = struct.unpack(">I", header)[0]
            if size <= 0 or size > MAX_FRAME_BYTES:
                return None
            body = b""
            while len(body) < size:
                chunk = sock.recv(size - len(body))
                if not chunk:
                    return None
                body += chunk
            response = json.loads(body.decode("utf-8"))
            result = response.get("result", {})
            if response.get("success") and isinstance(result, dict) and result.get("pong"):
                return result
            return None
        finally:
            sock.close()
    except Exception:
        return None


def _existing_listener_healthy():
    return _existing_listener_status() is not None


def _existing_listener_cad_safe(status):
    return (
        isinstance(status, dict)
        and status.get("cad_api_safe") is True
        and status.get("transport_only") is not True
    )


def _wait_for_listener_port_release(timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _listener_port_open():
            return True
        time.sleep(0.1)
    return False


def _write_stop_file():
    try:
        os.makedirs(STOP_DIR, exist_ok=True)
        with open(STOP_FILE, "w") as f:
            f.write("stop\n")
        return True
    except Exception:
        return False


def _report_existing_or_stale_listener():
    if not _listener_port_open():
        return False
    status = _existing_listener_status()
    if _existing_listener_cad_safe(status):
        _message("VW MCP Listener is already healthy on {h}:{p}".format(h=HOST, p=PORT))
        return True
    if status:
        if _write_stop_file():
            _message(
                "VW MCP port {h}:{p} is owned by a transport-only or CAD-unsafe listener. "
                "A STOP file was written so the dialog agent-session launcher can replace it.".format(
                    h=HOST, p=PORT
                )
            )
            if _wait_for_listener_port_release():
                return False
            _message(
                "VW MCP port {h}:{p} is still busy after STOP. Restart Vectorworks, "
                "then run this launcher again.".format(h=HOST, p=PORT)
            )
        else:
            _message(
                "VW MCP port {h}:{p} is owned by a CAD-unsafe listener, and the STOP file "
                "could not be written. Restart Vectorworks, then run this launcher again.".format(
                    h=HOST, p=PORT
                )
            )
        return True
    if _write_stop_file():
        _message(
            "VW MCP port {h}:{p} is open but not answering. "
            "A STOP file was written; wait a few seconds, then restart Vectorworks "
            "if the port still times out.".format(h=HOST, p=PORT)
        )
    else:
        _message(
            "VW MCP port {h}:{p} is open but not answering, and the STOP file "
            "could not be written. Restart Vectorworks, then run this launcher again.".format(
                h=HOST, p=PORT
            )
        )
    return True


def start_background():
    global _DISPATCH_MODE
    thread = getattr(_STATE, "listener_thread", None)
    if thread is not None and thread.is_alive():
        _message("VW MCP Listener is already running on {h}:{p}".format(h=HOST, p=PORT))
        return

    if _report_existing_or_stale_listener():
        return

    _DISPATCH_MODE = "background"
    thread = threading.Thread(
        target=main,
        kwargs={"show_alerts": False},
        name="VW MCP Listener",
        daemon=True,
    )
    _STATE.listener_thread = thread
    thread.start()
    _message(
        "VW MCP Listener starting in background on {h}:{p}. "
        "Use vw_ping from Claude Code to confirm.".format(h=HOST, p=PORT)
    )


def _stop_win_timer(show_message=True):
    global _DISPATCH_MODE
    timer_id = getattr(_STATE, "win_timer_id", None)
    user32 = getattr(_STATE, "win_timer_user32", None)
    server = getattr(_STATE, "win_timer_server", None)

    if timer_id and user32 is not None:
        try:
            user32.KillTimer(None, timer_id)
        except Exception:
            pass
    if server is not None:
        try:
            server.close()
        except Exception:
            pass

    _STATE.win_timer_id = None
    _STATE.win_timer_callback = None
    _STATE.win_timer_server = None
    _STATE.win_timer_user32 = None
    _STATE.win_timer_busy = False
    if _DISPATCH_MODE == "win_timer":
        _DISPATCH_MODE = None

    if show_message:
        _message("VW MCP Listener stopped.")


def start_win_timer():
    global _DISPATCH_MODE
    if getattr(_STATE, "win_timer_id", None):
        if _existing_listener_healthy():
            _message("VW MCP Listener is already running on {h}:{p}".format(h=HOST, p=PORT))
            return
        _stop_win_timer(show_message=False)

    if _report_existing_or_stale_listener():
        return

    if os.name != "nt":
        _message("VW MCP win_timer mode is only available on Windows; starting background mode instead.")
        start_background()
        return

    try:
        import ctypes
    except Exception as e:
        _alert("VW MCP could not import ctypes for Windows timer mode:\n{e}".format(e=e))
        return

    _DISPATCH_MODE = "win_timer"
    server = _ListenerServer(show_alerts=False)
    if not server.start():
        _DISPATCH_MODE = None
        _alert("VW MCP Listener could not start on {h}:{p}.".format(h=HOST, p=PORT))
        return

    timer_proc_type = ctypes.WINFUNCTYPE(
        None,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_size_t,
        ctypes.c_uint,
    )
    user32 = ctypes.windll.user32
    user32.SetTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint, timer_proc_type]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.KillTimer.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
    user32.KillTimer.restype = ctypes.c_int

    def timer_callback(hwnd, msg, event_id, time_ms):
        if getattr(_STATE, "win_timer_busy", False):
            return
        _STATE.win_timer_busy = True
        try:
            current = getattr(_STATE, "win_timer_server", None)
            if current is None or not current.pump(iterations=8, timeout=0.0):
                _stop_win_timer(show_message=False)
        except Exception as e:
            try:
                _message("VW MCP Listener timer stopped after error: {e}".format(e=e))
            except Exception:
                pass
            _stop_win_timer(show_message=False)
        finally:
            if getattr(_STATE, "win_timer_id", None):
                _STATE.win_timer_busy = False

    callback = timer_proc_type(timer_callback)
    timer_id = user32.SetTimer(None, 0, DIALOG_TIMER_MS, callback)
    if not timer_id:
        server.close()
        _DISPATCH_MODE = None
        _alert("VW MCP could not create a Windows timer for listener pumping.")
        return

    _STATE.win_timer_id = timer_id
    _STATE.win_timer_callback = callback
    _STATE.win_timer_server = server
    _STATE.win_timer_user32 = user32
    _STATE.win_timer_busy = False
    _message(
        "VW MCP Listener running on {h}:{p}. Vectorworks remains usable; use vw_ping to confirm.".format(
            h=HOST, p=PORT
        )
    )


def _set_dialog_text(dialog_id, item_id, text):
    try:
        if hasattr(vs, "SetControlText"):
            vs.SetControlText(dialog_id, item_id, text)
        elif hasattr(vs, "SetItemText"):
            vs.SetItemText(dialog_id, item_id, text)
    except Exception:
        pass


def start_dialog():
    global _DISPATCH_MODE
    if getattr(_STATE, "dialog_running", False):
        _message("VW MCP Listener dialog is already running.")
        return
    if _report_existing_or_stale_listener():
        return

    required = (
        "CreateLayout",
        "CreateStaticText",
        "SetFirstLayoutItem",
        "SetBelowItem",
        "RunLayoutDialog",
        "RegisterDialogForTimerEvents",
        "DeregisterDialogFromTimerEvents",
    )
    missing = [name for name in required if vs is None or not hasattr(vs, name)]
    if missing:
        _alert(
            "VW MCP dialog-pump mode needs Vectorworks dialog APIs that are not available:\n{m}".format(
                m=", ".join(missing)
            )
        )
        return

    _DISPATCH_MODE = "dialog"
    server = _ListenerServer(show_alerts=False)
    if not server.start():
        _DISPATCH_MODE = None
        _alert("VW MCP Listener could not start on {h}:{p}.".format(h=HOST, p=PORT))
        return

    dialog_id = None
    timer_registered = [False]
    status_item = 4
    hint_item = 5
    setup_event = getattr(vs, "SetupDialogC", 12255)

    try:
        dialog_id = vs.CreateLayout("VW MCP Listener", False, "Stop", "")
        vs.CreateStaticText(
            dialog_id,
            status_item,
            "Listening on {h}:{p} - version {v}".format(h=HOST, p=PORT, v=__VERSION__),
            48,
        )
        vs.CreateStaticText(
            dialog_id,
            hint_item,
            "Keep this dialog open while Claude Code controls Vectorworks.",
            56,
        )
        vs.SetFirstLayoutItem(dialog_id, status_item)
        vs.SetBelowItem(dialog_id, status_item, hint_item, 0, 0)

        def dialog_handler(item, data):
            if item == setup_event:
                try:
                    vs.RegisterDialogForTimerEvents(dialog_id, DIALOG_TIMER_MS)
                    timer_registered[0] = True
                except Exception as e:
                    _set_dialog_text(dialog_id, status_item, "Timer registration failed: {e}".format(e=e))
                server.pump(iterations=4, timeout=0.0)
                return

            if item in (1, 2):
                global _SHOULD_STOP
                _SHOULD_STOP = True
                return

            if not server.pump(iterations=8, timeout=0.0):
                if timer_registered[0]:
                    try:
                        vs.DeregisterDialogFromTimerEvents(dialog_id)
                    except Exception:
                        pass
                    timer_registered[0] = False
                _set_dialog_text(dialog_id, status_item, "Stopped. Close this dialog to finish.")

        _STATE.dialog_running = True
        _message("VW MCP Listener dialog running on {h}:{p}".format(h=HOST, p=PORT))
        vs.RunLayoutDialog(dialog_id, dialog_handler)
    finally:
        if dialog_id is not None and timer_registered[0]:
            try:
                vs.DeregisterDialogFromTimerEvents(dialog_id)
            except Exception:
                pass
        server.close()
        _STATE.dialog_running = False
        _message("VW MCP Listener stopped.")


def _autostart_mode():
    mode = os.environ.get("VW_MCP_MODE", "").strip().lower()
    if mode:
        return mode
    if os.environ.get("VW_MCP_BACKGROUND", "").lower() in ("1", "true", "yes"):
        return "dialog"
    return "dialog"


if os.environ.get("VW_MCP_NO_AUTOSTART", "").lower() not in ("1", "true", "yes"):
    _mode = _autostart_mode()
    if _mode in ("win_timer", "wintimer", "windows_timer", "windows-timer"):
        start_win_timer()
    elif _mode in ("dialog", "dialog_timer", "timer", "mainthread", "main-thread"):
        start_dialog()
    elif _mode in ("background", "thread"):
        start_background()
    else:
        main()
