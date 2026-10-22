from __future__ import annotations

import hmac
import json
import secrets
from http.cookies import SimpleCookie

from browser_recovery import register_service, service
from reliability_config import load_reliability, public_url
from scan_progress import ProgressStore


PAGE = """<!doctype html><html lang="en"><meta name="viewport" content="width=device-width, initial-scale=1"><title>OLX manual verification</title>
<style>body{font:16px system-ui;background:#f5f5f4;color:#202020;margin:0;padding:20px;max-width:1100px}button,input{font:inherit;padding:12px;margin:6px 0;border:1px solid #ccc;border-radius:8px}button{background:#202020;color:white;cursor:pointer}img{max-width:100%;border:1px solid #ddd;touch-action:manipulation}#view{display:none}.tools{display:flex;gap:8px;flex-wrap:wrap}#message{padding:12px 0}input{flex:1;min-width:120px}</style>
<h1>Manual OLX verification</h1><p>This page controls only the bot's OLX verification tab. Complete the website's verification yourself. Your scan resumes after the normal listing page is detected.</p>
<button id="start">Open verification session</button><div id="message" role="status"></div><div id="view"><img id="screen" alt="Bot verification browser"><div class="tools"><button id="up">Scroll up</button><button id="down">Scroll down</button><button id="tab">Tab</button><button id="enter">Enter</button><button id="back">Backspace</button></div><div class="tools"><input id="text" autocomplete="off" placeholder="Type into the selected field"><button id="type">Send text</button></div></div>
<script nonce="NONCE">
const token=location.hash.slice(1);history.replaceState(null,'',location.pathname);let done=false;let busy=false;
const message=document.querySelector('#message');const screen=document.querySelector('#screen');
async function post(path,body){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw Error(data.error||'Request failed');return data}
function finish(){done=true;message.textContent='Verification succeeded. Your saved scan will resume automatically.';document.querySelector('#view').style.display='none'}
async function frame(){if(done||busy)return;busy=true;try{const response=await fetch('/recovery/frame',{cache:'no-store'});if(!response.ok)throw Error('Session expired. Send /verify for a fresh link.');if(response.headers.get('Content-Type').includes('json')){const data=await response.json();if(data.verified)finish()}else{const blob=await response.blob();const prior=screen.src;screen.src=URL.createObjectURL(blob);if(prior.startsWith('blob:'))URL.revokeObjectURL(prior)}}catch(error){done=true;message.textContent=error.message}finally{busy=false}}
document.querySelector('#start').onclick=async()=>{try{message.textContent='Opening your bot session…';await post('/recovery/claim',{token});document.querySelector('#start').style.display='none';document.querySelector('#view').style.display='block';message.textContent='Tap the browser image to interact with the verification.';await frame();setInterval(frame,1500)}catch(error){message.textContent=error.message}};
async function input(values){try{const data=await post('/recovery/input',values);if(data.verified)finish();else await frame()}catch(error){message.textContent=error.message}}
screen.onclick=event=>{const box=screen.getBoundingClientRect();input({type:'click',x:(event.clientX-box.left)*1024/box.width,y:(event.clientY-box.top)*768/box.height})};
document.querySelector('#up').onclick=()=>input({type:'scroll',delta:-500});document.querySelector('#down').onclick=()=>input({type:'scroll',delta:500});
for(const [id,key]of[['tab','Tab'],['enter','Enter'],['back','Backspace']])document.querySelector('#'+id).onclick=()=>input({type:'key',key});
document.querySelector('#type').onclick=()=>{const field=document.querySelector('#text');input({type:'text',text:field.value});field.value=''};
</script></html>"""


def send(handler, status, payload, content_type="application/json", cookie=""):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    if cookie:
        handler.send_header("Set-Cookie", cookie)
    handler.end_headers()
    handler.wfile.write(body)


def session_incident(handler, progress, touch=True):
    cookie = SimpleCookie()
    cookie.load(handler.headers.get("Cookie", ""))
    token = cookie.get("olx_verification")
    if not token:
        raise PermissionError("Verification session required")
    return progress.authorize(token.value, load_reliability().verification_idle_seconds, touch=touch)


def dispatch(handler, path, method):
    if path != "/verify" and not path.startswith("/recovery/"):
        return False
    try:
        if path == "/verify" and method == "GET":
            nonce = secrets.token_urlsafe(18)
            body = PAGE.replace("NONCE", nonce).encode()
            handler.send_response(200)
            handler.send_header("Content-Type", "text/html; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("Referrer-Policy", "no-referrer")
            handler.send_header("X-Frame-Options", "DENY")
            handler.send_header("Content-Security-Policy", f"default-src 'self'; script-src 'nonce-{nonce}'; style-src 'unsafe-inline'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            handler.end_headers()
            handler.wfile.write(body)
            return True
        progress = ProgressStore()
        if method == "POST":
            length = int(handler.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise ValueError("Invalid request size")
            values = json.loads(handler.rfile.read(length))
            if not isinstance(values, dict):
                raise ValueError("Invalid request body")
            if path == "/recovery/internal":
                supplied = handler.headers.get("X-Recovery-Internal", "")
                expected = progress.get("browser_service_secret", "")
                if not expected or not hmac.compare_digest(supplied, expected):
                    raise PermissionError("Unauthorized")
                result = service().call("fetch", **values)
                send(handler, 200, result)
                return True
            expected_origin = public_url()
            if not expected_origin or handler.headers.get("Origin", "") != expected_origin:
                raise PermissionError("Invalid request origin")
            if path == "/recovery/claim":
                config = load_reliability()
                session, incident = progress.redeem(str(values.get("token", "")), config.verification_max_seconds)
                service().call("start", incident_id=incident)
                send(handler, 200, {"started": True}, cookie=f"olx_verification={session}; Secure; HttpOnly; SameSite=Strict; Path=/recovery/; Max-Age={config.verification_max_seconds}")
                return True
            if path == "/recovery/input":
                incident = session_incident(handler, progress)
                result = service().call("input", incident_id=incident, **{key: value for key, value in values.items() if key in {"type", "x", "y", "delta", "text", "key"}})
                send(handler, 200, result)
                return True
        if path == "/recovery/frame" and method == "GET":
            incident = session_incident(handler, progress, touch=False)
            result = service().call("frame", incident_id=incident)
            send(handler, 200, result, "image/jpeg" if isinstance(result, bytes) else "application/json")
            return True
        send(handler, 404, {"error": "Unknown recovery route"})
    except PermissionError as error:
        send(handler, 401, {"error": str(error)})
    except (ValueError, json.JSONDecodeError):
        send(handler, 400, {"error": "Invalid verification request"})
    except Exception:
        send(handler, 503, {"error": "Verification browser unavailable. Progress is saved; send /verify to retry."})
    return True
