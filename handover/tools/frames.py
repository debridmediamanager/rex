"""Cycle 33 driver: capture the frames one write pushes, on one server."""
import json, os, socket, sys, threading, time, urllib.request, urllib.parse
def capture(host, port, tok, action, seconds=12.0):
    s=socket.create_connection((host,port), timeout=20)
    s.sendall(('GET /:/eventsource/notifications?X-Plex-Token=%s HTTP/1.1\r\nHost: %s:%d\r\nAccept: text/event-stream\r\n\r\n'%(tok,host,port)).encode())
    buf=[b'']
    def read():
        try:
            while True:
                d=s.recv(65536)
                if not d: break
                buf[0]+=d
        except Exception: pass
    t=threading.Thread(target=read, daemon=True); t.start()
    time.sleep(1.5)
    action()
    time.sleep(seconds)
    try: s.close()
    except Exception: pass
    text=buf[0].decode('utf-8','replace')
    body=text.split('\r\n\r\n',1)[1] if '\r\n\r\n' in text else text
    out=[]
    kind=None
    for line in body.split('\n'):
        if line.startswith('event:'): kind=line.split(':',1)[1].strip()
        elif line.startswith('data:'):
            try: row=json.loads(line.split(':',1)[1].strip())
            except Exception: continue
            out.append((kind,row))
    return out
def req(base, tok, method, path):
    r=urllib.request.Request(base+path, headers={'X-Plex-Token':tok,'Accept':'application/json','User-Agent':'curl/8'}, method=method)
    with urllib.request.urlopen(r, timeout=30) as resp:
        b=resp.read(); return resp.status, (json.loads(b) if b[:1]==b'{' else b)
def summary(frames):
    out=[]
    for kind,row in frames:
        if kind=='timeline':
            e=row.get('TimelineEntry',{})
            out.append('timeline itemID=%s type=%s state=%s%s' % (e.get('itemID'), e.get('type'), e.get('state'), (' '+e['metadataState']) if e.get('metadataState') else ''))
        elif kind=='activity':
            a=row.get('ActivityNotification',{}); act=a.get('Activity',{})
            out.append('activity %s %s %s%%' % (a.get('event'), act.get('type'), act.get('progress')))
        else:
            out.append(kind)
    return out
