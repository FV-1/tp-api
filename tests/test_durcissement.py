"""Tests du durcissement et des limites (bloc D, partie Victor), contre sandboxd lancé.

    .venv/bin/python -m tests.test_durcissement            # durcissement + liens symboliques
    .venv/bin/python -m tests.test_durcissement --quota 3  # + quota (sandboxd lancé avec SANDBOXD_MAX_SANDBOXES=3)
    .venv/bin/python -m tests.test_durcissement --ttl      # + expiration (≈ 70 s)

SANDBOXD_URL (défaut http://localhost:8000) et SANDBOXD_API_KEY (si l'authentification est active).
Code de sortie 0 si tout passe.
"""
import os
import sys
import time

import httpx

API = os.environ.get("SANDBOXD_URL", "http://localhost:8000")
KEY = os.environ.get("SANDBOXD_API_KEY")
client = httpx.Client(base_url=API, timeout=90,
                      headers={"Authorization": f"Bearer {KEY}"} if KEY else {})
failures = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(f"  {'OK' if ok else 'KO'}  {label}" + (f"  ({detail})" if detail and not ok else ""))


def create(ttl: int = 600) -> str:
    r = client.post("/v1/sandboxes", json={"image": "alpine:3.20", "ttl_seconds": ttl})
    r.raise_for_status()
    sid = r.json()["id"]
    for _ in range(120):
        if client.get(f"/v1/sandboxes/{sid}").json()["status"] != "pending":
            break
        time.sleep(0.5)
    return sid


def run(sid: str, *cmd: str, timeout: int = 10) -> dict:
    r = client.post(f"/v1/sandboxes/{sid}/exec", json={"cmd": list(cmd), "timeout_seconds": timeout})
    r.raise_for_status()
    return r.json()


def hardening(sid: str) -> None:
    print("Conteneur durci")
    r = run(sid, "id", "-u")
    check("utilisateur non root", r["stdout"].strip() not in ("", "0"), r["stdout"])
    r = run(sid, "sh", "-c", "grep -E '^(CapEff|NoNewPrivs)' /proc/self/status")
    check("aucune capability", "CapEff:\t0000000000000000" in r["stdout"], r["stdout"])
    check("no-new-privileges", "NoNewPrivs:\t1" in r["stdout"], r["stdout"])
    r = run(sid, "sh", "-c", "touch /etc/pirate || touch /usr/pirate || touch /pirate")
    check("écriture hors de /workspace refusée", r["exit_code"] != 0, r["stderr"])
    r = run(sid, "sh", "-c", "echo ok > /workspace/a && echo ok > /tmp/b && cat /workspace/a /tmp/b")
    check("/workspace et /tmp inscriptibles", r["stdout"] == "ok\nok\n", r["stderr"])
    r = run(sid, "sh", "-c", "wget -q -T 3 -O- http://1.1.1.1 || nc -w 3 1.1.1.1 80 </dev/null")
    check("réseau coupé", r["exit_code"] != 0, r["stdout"])
    r = run(sid, "sh", "-c", "ip -o link 2>/dev/null || cat /proc/net/dev")
    check("aucune interface réseau hors loopback", "eth0" not in r["stdout"], r["stdout"])
    r = run(sid, "sh", "-c", "dd if=/dev/zero of=/workspace/gros bs=1M count=200 2>&1; rm -f /workspace/gros")
    check("taille de /workspace bornée", "No space left" in r["stdout"], r["stdout"][-120:])


def fork_bomb(witness: str) -> None:
    """Dans une sandbox à part : la bombe la sature, mais ne déborde ni sur l'hôte ni sur les autres."""
    print("Fork bomb")
    sid = create()
    t0 = time.monotonic()
    r = run(sid, "sh", "-c", "f() { f | f & }; f; sleep 2; echo fin", timeout=10)
    check("l'exec rend la main malgré la bombe", time.monotonic() - t0 < 30, f"{time.monotonic() - t0:.0f} s")
    check("la bombe se heurte à la limite de processus",
          "fork" in (r["stdout"] + r["stderr"]).lower() or r["exit_code"] != 0, r["stderr"][-120:])
    r = run(witness, "echo", "toujours là")
    check("une autre sandbox n'est pas touchée", r["stdout"] == "toujours là\n", r["stderr"])
    r = client.delete(f"/v1/sandboxes/{sid}")
    check("la sandbox saturée se détruit (204)", r.status_code == 204, f"{r.status_code}")


def symlinks(sid: str) -> None:
    print("Liens symboliques")
    run(sid, "sh", "-c", "ln -s /etc /workspace/etc; ln -s /etc/passwd /workspace/pw; mkdir -p /workspace/d")
    for path in ("etc/passwd", "pw"):
        r = client.get(f"/v1/sandboxes/{sid}/files", params={"path": path})
        check(f"lecture de {path} via un lien refusée", r.status_code == 422, f"{r.status_code} {r.text[:80]}")
    r = client.put(f"/v1/sandboxes/{sid}/files", params={"path": "etc/pirate"}, content=b"x",
                   headers={"Content-Type": "application/octet-stream"})
    check("écriture via un lien refusée", r.status_code == 422, f"{r.status_code} {r.text[:80]}")
    r = client.put(f"/v1/sandboxes/{sid}/files", params={"path": "d/ok.txt"}, content=b"salut",
                   headers={"Content-Type": "application/octet-stream"})
    check("écriture normale acceptée", r.status_code == 201, f"{r.status_code} {r.text[:80]}")
    r = run(sid, "sh", "-c", "echo ' modifié' >> /workspace/d/ok.txt && cat /workspace/d/ok.txt")
    check("fichier écrit par l'API modifiable par la sandbox", r["stdout"] == "salut modifié\n", r["stderr"])


def quota(limit: int, first: str) -> None:
    print(f"Quota ({limit} sandboxes)")
    extra = []
    for _ in range(limit - 1):
        extra.append(create())
    r = client.post("/v1/sandboxes", json={"image": "alpine:3.20"})
    check("création au-delà du quota refusée en 409", r.status_code == 409 and "quota" in r.text,
          f"{r.status_code} {r.text[:80]}")
    for sid in extra:
        client.delete(f"/v1/sandboxes/{sid}")
    r = client.post("/v1/sandboxes", json={"image": "alpine:3.20"})
    check("création possible après suppression", r.status_code == 202, f"{r.status_code}")
    if r.status_code == 202:
        client.delete(f"/v1/sandboxes/{r.json()['id']}")


def ttl() -> None:
    print("Durée de vie")
    sid = create(ttl=60)
    print("  … attente de l'expiration (≈ 70 s)")
    time.sleep(70)
    r = client.get(f"/v1/sandboxes/{sid}")
    check("sandbox expirée détruite (404)", r.status_code == 404, f"{r.status_code}")


def main() -> int:
    sid = create()
    try:
        hardening(sid)
        symlinks(sid)
        fork_bomb(sid)
        if "--quota" in sys.argv:
            quota(int(sys.argv[sys.argv.index("--quota") + 1]), sid)
    finally:
        client.delete(f"/v1/sandboxes/{sid}")
    if "--ttl" in sys.argv:
        ttl()
    print("Tout est bon." if not failures else f"{failures} échec(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
