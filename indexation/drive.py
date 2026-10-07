"""Envoi des videos vers Google Drive (multi-comptes avec rotation).

Version HTTP directe (lib `requests`) : pas de client lourd, pas de document
de decouverte a telecharger, proxy explicite. Robuste sur reseaux restreints.
"""
import json
import os
from pathlib import Path

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]
API_BASE = "https://www.googleapis.com/drive/v3"
UPLOAD_BASE = "https://www.googleapis.com/upload/drive/v3/files"
SAFETY_MARGIN_BYTES = 100 * 1024 * 1024  # marge de securite exigee en plus du fichier
TIMEOUT_GET = (30, 60)
TIMEOUT_UPLOAD = (30, 600)


class DriveError(Exception):
    pass


class DriveQuotaError(DriveError):
    """Quota depasse (message contient storageQuotaExceeded)."""
    pass


# ---------------------------------------------------------------------------
# Configuration locale (secrets, tokens, dossiers, mapping, etat)
# ---------------------------------------------------------------------------

def config_dir():
    from django.conf import settings
    d = Path(getattr(settings, "DRIVE_CONFIG_DIR"))
    (d / "secrets").mkdir(parents=True, exist_ok=True)
    (d / "tokens").mkdir(parents=True, exist_ok=True)
    return d


def secret_files():
    return sorted((config_dir() / "secrets").glob("*.json"))


def secret_path(account):
    files = secret_files()
    if not (1 <= account <= len(files)):
        raise ValueError(f"Compte {account} invalide (1-{len(files)})")
    return files[account - 1]


def token_path(account):
    return config_dir() / "tokens" / f"token_{account}.json"


def load_folders():
    with open(config_dir() / "folders.json", encoding="utf-8") as f:
        return json.load(f)


def mapping_path():
    return config_dir() / "drive_mapping.json"


def load_mapping():
    p = mapping_path()
    if not p.exists():
        return []
    return json.loads(p.read_text(encoding="utf-8"))


def save_mapping(mapping):
    mapping_path().write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")


def state_path():
    return config_dir() / "drive_state.json"


def load_state():
    p = state_path()
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"blocked": False, "last_account": 0, "reason": ""}


def save_state(state):
    state_path().write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _verifier_path(account):
    return config_dir() / f".verifier_{account}"


# ---------------------------------------------------------------------------
# OAuth (autorisation initiale, une fois par compte)
# ---------------------------------------------------------------------------

def build_flow(account):
    return InstalledAppFlow.from_client_secrets_file(
        str(secret_path(account)), SCOPES, redirect_uri="http://localhost"
    )


def auth_url(account):
    flow = build_flow(account)
    url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    # Persiste le code_verifier PKCE : l'echange du code se fera dans un
    # autre processus, qui doit presenter le meme verifier.
    _verifier_path(account).write_text(flow.code_verifier or "", encoding="utf-8")
    return url


def finish_auth(account, code):
    flow = build_flow(account)
    vp = _verifier_path(account)
    if vp.exists():
        flow.code_verifier = vp.read_text(encoding="utf-8").strip() or None
    flow.fetch_token(code=code.strip())
    try:
        vp.unlink()
    except OSError:
        pass
    token_path(account).write_text(flow.credentials.to_json(), encoding="utf-8")
    return flow.credentials


# ---------------------------------------------------------------------------
# Client API minimaliste
# ---------------------------------------------------------------------------

def _proxy_dict():
    proxy_url = os.environ.get("https_proxy") or os.environ.get("http_proxy")
    if proxy_url:
        return {"http": proxy_url, "https": proxy_url}
    return {}


def _load_creds(account):
    creds = Credentials.from_authorized_user_file(str(token_path(account)), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path(account).write_text(creds.to_json(), encoding="utf-8")
    if not creds.token:
        raise DriveError(f"Compte {account} : aucun jeton d'acces (re-autorisez le compte).")
    return creds


class DriveAPI:
    """Un compte Google Drive, appels REST directs."""

    def __init__(self, account):
        self.account = account
        self._creds = _load_creds(account)

    def _headers(self):
        return {"Authorization": f"Bearer {self._creds.token}"}

    def _call(self, method, url, retry_auth=True, **kwargs):
        kwargs.setdefault("timeout", TIMEOUT_GET)
        kwargs["proxies"] = _proxy_dict()
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self._creds.token}"
        try:
            resp = requests.request(method, url, headers=headers, **kwargs)
        except requests.RequestException as e:
            raise DriveError(f"Reseau : {e}")
        if resp.status_code == 401 and retry_auth and self._creds.refresh_token:
            try:
                self._creds.refresh(Request())
                token_path(self.account).write_text(self._creds.to_json(), encoding="utf-8")
            except Exception as e:
                raise DriveError(f"Re-authentification impossible : {e}")
            return self._call(method, url, retry_auth=False, **kwargs)
        if resp.status_code in (403, 429):
            body = resp.text[:300]
            if "storageQuotaExceeded" in body or "quotaExceeded" in body:
                raise DriveQuotaError(f"storageQuotaExceeded (compte {self.account})")
            raise DriveError(f"HTTP {resp.status_code} : {body}")
        if resp.status_code >= 400:
            raise DriveError(f"HTTP {resp.status_code} : {resp.text[:300]}")
        return resp

    def email(self):
        data = self._call("GET", f"{API_BASE}/about",
                          params={"fields": "user(emailAddress)"}).json()
        return data.get("user", {}).get("emailAddress", "?")

    def quota(self):
        q = self._call("GET", f"{API_BASE}/about",
                       params={"fields": "storageQuota"}).json().get("storageQuota", {})
        limit = q.get("limit")
        usage = int(q.get("usage") or 0)
        if limit is None:
            return {"limited": False, "free": None, "usage": usage, "limit": None}
        limit = int(limit)
        return {"limited": True, "free": limit - usage, "usage": usage, "limit": limit}

    def folder(self, folder_id):
        try:
            meta = self._call("GET", f"{API_BASE}/files/{folder_id}",
                              params={"fields": "id,name,mimeType,capabilities",
                                      "supportsAllDrives": "true"}).json()
        except DriveError as e:
            return {"ok": False, "error": str(e)[:200]}
        caps = meta.get("capabilities", {})
        return {"ok": True, "name": meta.get("name"), "writable": bool(caps.get("canAddChildren"))}

    def find(self, folder_id, name):
        safe = name.replace("'", "\\'")
        data = self._call(
            "GET", f"{API_BASE}/files",
            params={"q": f"name='{safe}' and '{folder_id}' in parents and trashed=false",
                    "fields": "files(id,name)",
                    "supportsAllDrives": "true"}).json()
        files = data.get("files", [])
        return files[0]["id"] if files else None

    def list_files(self, folder_id, page_size=100):
        """Toutes les videos d'un dossier (liste paginee)."""
        out = []
        page_token = None
        while True:
            params = {"q": f"'{folder_id}' in parents and trashed=false",
                      "fields": "files(id,name,size,modifiedTime,videoMediaMetadata(width,height,durationMillis)),nextPageToken",
                      "orderBy": "modifiedTime desc",
                      "pageSize": page_size,
                      "supportsAllDrives": "true"}
            if page_token:
                params["pageToken"] = page_token
            data = self._call("GET", f"{API_BASE}/files", params=params).json()
            out.extend(data.get("files", []))
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return out

    def trash(self, file_id):
        """Met un fichier a la corbeille (recuperable 30 jours)."""
        self._call("PATCH", f"{API_BASE}/files/{file_id}",
                   params={"supportsAllDrives": "true"},
                   json_body={"trashed": True})
        return True

    def upload(self, folder_id, local_path, drive_name, update_id=None):
        """Envoi avec reprise (resumable). Retourne l'ID du fichier."""
        size = Path(local_path).stat().st_size
        if update_id:
            # Mise a jour du contenu : PATCH (PUT renvoie 404 sur cette route)
            with open(local_path, "rb") as f:
                resp = self._call(
                    "PATCH", f"{UPLOAD_BASE}/{update_id}",
                    params={"uploadType": "media", "supportsAllDrives": "true"},
                    headers={"Content-Type": "video/mp4"},
                    data=f, timeout=TIMEOUT_UPLOAD)
            return resp.json().get("id")
        init = self._call(
            "POST", UPLOAD_BASE,
            params={"uploadType": "resumable", "supportsAllDrives": "true"},
            headers={"Content-Type": "application/json; charset=UTF-8",
                     "X-Upload-Content-Type": "video/mp4",
                     "X-Upload-Content-Length": str(size)},
            json={"name": drive_name, "parents": [folder_id]})
        session_uri = init.headers.get("Location")
        if not session_uri:
            raise DriveError("Session d'upload non fournie par Google.")
        with open(local_path, "rb") as f:
            done = self._call("PUT", session_uri, headers={"Content-Type": "video/mp4"},
                              data=f, timeout=TIMEOUT_UPLOAD)
        return done.json().get("id")


# Anciens noms conserves pour les commandes existantes
def quota_info(api):
    return api.quota()


def check_folder(api, folder_id):
    return api.folder(folder_id)


def account_email(api):
    return api.email()


def upload_video(api, folder_id, local_path, drive_name):
    return api.upload(folder_id, local_path, drive_name)


def get_service(account):
    return DriveAPI(account)


# ---------------------------------------------------------------------------
# Envoi avec rotation stricte 1 -> 2 -> 3 -> 4
# ---------------------------------------------------------------------------

def send_to_drive(local_path, drive_name, log):
    """Envoie une video sur le compte prioritaire ayant la place.

    Ordre strict : compte 1, puis 2, 3, 4. Un compte inferieur n'est utilise
    que si tous les comptes au-dessus sont pleins. A chaque video, on
    re-verifie depuis le compte 1 (une place liberee est donc reutilisee).
    Si le fichier existe deja dans le dossier, succes immediat (anti-doublon).
    Retourne (numero_compte, file_id) ou (None, "sature"/"erreur"/"config...").
    """
    mapping = load_mapping()
    if not mapping:
        return None, "config: mapping Drive absent (lancez : python manage.py drive_probe)"
    mapping = sorted(mapping, key=lambda m: m["account"])
    state = load_state()
    size = Path(local_path).stat().st_size
    tried = 0
    quota_blocks = 0
    for entry in mapping:
        account = entry["account"]
        try:
            api = DriveAPI(account)
        except Exception as e:
            log(f"[Drive] Compte {account} : auth impossible ({str(e)[:120]}) : je passe au suivant.")
            tried += 1
            continue
        try:
            already = api.find(entry["folder_id"], drive_name)
        except DriveError as e:
            log(f"[Drive] Compte {account} : verification impossible ({str(e)[:120]}) : suivant.")
            tried += 1
            continue
        if already:
            log(f"[Drive] {drive_name} deja sur le compte {account} : pas de doublon.")
            state["last_account"] = account
            state["blocked"] = False
            state["reason"] = ""
            save_state(state)
            return account, already
        try:
            q = api.quota()
        except DriveError as e:
            log(f"[Drive] Compte {account} : quota illisible ({str(e)[:120]}) : suivant.")
            tried += 1
            continue
        if q["limited"] and q["free"] < size + SAFETY_MARGIN_BYTES:
            log(f"[Drive] Compte {account} ({entry.get('email')}) plein "
                f"({q['free'] // 1024 // 1024} Mo libres) : suivant.")
            tried += 1
            quota_blocks += 1
            continue
        try:
            file_id = api.upload(entry["folder_id"], local_path, drive_name)
        except DriveQuotaError:
                log(f"[Drive] Compte {account} plein pendant l'envoi : suivant.")
                tried += 1
                quota_blocks += 1
                continue
        except DriveError:
            raise
        state["last_account"] = account
        state["blocked"] = False
        state["reason"] = ""
        save_state(state)
        return account, file_id
    if tried > 0 and quota_blocks == tried:
        return None, "sature"
    return None, "erreur"


def flush_pending_videos(log=None):
    """Envoie toutes les videos gardees en local. Retourne (envoyees, gardees, probleme).
    Utilise par le bouton Reessayer et par l'auto-deblocage a l'upload."""
    from django.conf import settings
    videos_dir = Path(getattr(settings, "VIDEOS_DIR"))
    pending = sorted(videos_dir.glob("video_*.mp4"), key=lambda p: p.name) if videos_dir.exists() else []
    sent, kept, problem = 0, 0, ""
    for local_path in pending:
        try:
            account_used, result = send_to_drive(local_path, local_path.name, log or (lambda m: None))
        except Exception as e:
            problem = str(e)[:150]
            kept += 1
            continue
        if account_used:
            try:
                local_path.unlink()
                sent += 1
            except OSError:
                sent += 1
        else:
            problem = str(result)
            kept += 1
    return sent, kept, problem


def backup_site_json(log=None):
    """Sauvegarde json_du_site.json dans le dossier prevu (compte 1).
    Mise a jour si deja present, creation sinon. Ne leve jamais (log only).
    Desactivee si YANABOU_NO_BACKUP=1 (tests)."""
    if os.environ.get("YANABOU_NO_BACKUP"):
        return None
    from django.conf import settings
    site_json = Path(getattr(settings, "SITE_JSON_PATH"))
    folder_id = getattr(settings, "DRIVE_BACKUP_FOLDER_ID", "")
    if not site_json.exists() or not folder_id:
        return None
    try:
        api = DriveAPI(1)
        existing = api.find(folder_id, site_json.name)
        if existing:
            file_id = api.upload(folder_id, site_json, site_json.name, update_id=existing)
            if log:
                log("[Backup] JSON mis a jour sur Drive (compte 1).", level="info")
            return file_id
        file_id = api.upload(folder_id, site_json, site_json.name)
        if log:
            log("[Backup] JSON sauvegarde sur Drive (compte 1).", level="info")
        return file_id
    except Exception as e:
        if log:
            log(f"[Backup] ECHEC : {str(e)[:150]} (suite du traitement OK).", level="error")
        return None
