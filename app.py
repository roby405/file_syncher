from mimetypes import init
import socket
import os
import time
import json
import hashlib
import threading
from zeroconf import Zeroconf, ServiceInfo, ServiceBrowser, ServiceListener
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler



class Peer:
    def __init__(self, folder_path, port=12405, interval=10):
        self.folder = folder_path
        self.port = port
        self.interval = interval
        self.peers = {}
        self.manifest = self._generate_manifest()
        self.zc = Zeroconf()
        self.info = ServiceInfo(
            "_p2psync._tcp.local.",
            f"p2psync_{socket.gethostname()}._p2psync._tcp.local.",
            addresses=[socket.inet_aton(socket.gethostbyname(socket.gethostname()))],
            port=self.port,
            properties={
                "folder": self.folder,
            }
        )
        self._start_discovery()
        self._start_monitoring()
        self._start_server()
        self._start_sync_loop()
    
    # i got some theories this is slow
    def _generate_manifest(self):
        manifest = {}
        for root, _, files in os.walk(self.folder):
            rel_root = os.path.relpath(root, self.folder)
            for file in files:
                filepath = os.path.join(root, file)
                rel_path = os.path.join(rel_root, file)
                stat = os.stat(filepath)
                hash_sha = hashlib.sha256(open(filepath, 'rb').read()).hexdigest()
                manifest[rel_path] = {
                    'size': stat.st_size,
                    'mtime': stat.st_mtime,
                    'hash': hash_sha
                }
        return manifest
    
    def _start_discovery(self):
        self.zc.register_service(self.info)
        self.listener = ZcListener(self)
        # self.zc.add_service_listener("_p2psync._tcp.local.", listener)
        self.browser = ServiceBrowser(self.zc, "_p2psync._tcp.local.", listener=self.listener)
    
    def _start_server(self):
        threading.Thread(target=self._handle_connections, daemon=True).start()
      
    def _handle_connections(self):
        sock = socket.socket()
        sock.bind(("0.0.0.0", self.port))
        sock.listen(5)
        while True:
            client, addr = sock.accept()
            try:
                print(f"Connection from {addr}")
                data = json.loads(client.recv(4096).decode("utf-8"))
                if data['type'] == 'manifest':
                    self.peers[addr[0]] = {
                        "manifest": data["data"],
                        "last_seen": time.time()
                    }
                    client.send(json.dumps({"type": "manifest", "data": self.manifest}).encode("utf-8"))
                elif data["type"] == "update":
                    for file in data["data"]:
                        if data["action"] == "add" or data["action"] == "modify":
                            with open(os.path.join(self.folder, file["path"]), "wb") as f:
                                f.write(file["content"])
                        elif data["action"] == "delete":
                            os.remove(os.path.join(self.folder, file["path"]))
                    client.send(json.dumps({"status": "ok"}).encode("utf-8"))
                self.manifest = self._generate_manifest()
            except Exception as e:
                print(f"Error handling connection from {addr}: {e}")
            finally:
                client.close()   
    
    def _sync(self):
        while True:
            self.manifest = self._generate_manifest()
            for ip in list(self.peers.keys()):
                try:
                    sock = socket.socket()
                    sock.connect((ip, self.port))
                    sock.send(json.dumps({"type": "manifest", "data": self.manifest}).encode("utf-8"))
                    res = json.loads(sock.recv(4096).decode("utf-8"))
                    if res["type"] == "manifest":
                        peer_manifest = res["data"]
                        self.peers[ip]["manifest"] = peer_manifest
                        self.peers[ip]["last_seen"] = time.time()
                        updates = self._check_for_updates(peer_manifest)
                        print(self.manifest)
                        print("that's mine ^")
                        print(peer_manifest)
                        if updates:
                            sock.send(json.dumps({"type": "update", "data": updates}).encode("utf-8"))
                            ack = json.loads(sock.recv(1024).decode("utf-8"))
                            if ack.get("status") == "ok":
                                print(f"Synced with {ip}")
                    sock.close()
                except Exception as e:
                    print(f"Error syncing with {ip}: {e}")
                    del self.peers[ip]
            time.sleep(self.interval)
    
    def _start_sync_loop(self):
        threading.Thread(target=self._sync, daemon=True).start()
    
    # also might be slow
    def _check_for_updates(self, peer_manifest):
        updates = []
        # Check for adds/modifies (local has newer or missing in remote)
        for rel_path, local_info in self.manifest.items():
            remote_info = peer_manifest.get(rel_path)
            if not remote_info or local_info['mtime'] > remote_info['mtime'] or local_info['hash'] != remote_info['hash']:
                full_path = os.path.join(self.folder, rel_path)
                with open(full_path, 'rb') as f:
                    content = f.read()
                updates.append({
                    'action': 'add' if not remote_info else 'modify',
                    'path': rel_path,
                    'content': content,
                    'mtime': local_info['mtime'],
                    'hash': local_info['hash']
                })
        # Check for deletes (remote has files not in local)
        for rel_path in set(peer_manifest) - set(self.manifest):
            updates.append({
                'action': 'delete',
                'path': rel_path
            })
        return updates
    
    
    def _start_monitoring(self):
        class Handler(FileSystemEventHandler):
            def __init__(self, outer_self):
                self.outer_self = outer_self

            def on_any_event(self, event):
                if not event.is_directory:
                    self.outer_self.manifest = self.outer_self._generate_manifest()
                    print(f"Folder change detected: {event.event_type}")
        
        observer = Observer()
        observer.schedule(Handler(self), self.folder, recursive=True)
        observer.start()
        # unknown if it needs a thread
    
    def shutdown(self):
        self.zc.unregister_service(self.info)
        self.zc.close()

class ZcListener(ServiceListener):
    def __init__(self, peer):
        self.peer = peer
        
    def add_service(self, zc, type, name):
        info = zc.get_service_info(type, name)
        if info:
            ip = socket.inet_ntoa(info.addresses[0])
            if ip != socket.gethostbyname(socket.gethostname()):
                self.peer.peers[ip] = {
                    "last_seen": time.time()
                }
                print(f"Discovered peer: {ip}")

    def remove_service(self, zc, type, name):
        info = zc.get_service_info(type, name)
        if info:
            ip = socket.inet_ntoa(info.addresses[0])
            if ip in self.peer.peers:
                del self.peer.peers[ip]
                print(f"Lost peer: {ip}")
                
    def update_service(self, zc, type, name):
        pass

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python p2p_sync.py <folder_path>")
        sys.exit(1)
    folder = sys.argv[1]
    peer = Peer(folder)
    print(f"Peer started for folder: {folder}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        peer.shutdown()
        print("Shutdown complete")
    