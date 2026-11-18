"""
Monitor de anúncios do Facebook Marketplace com aviso no WhatsApp (CallMeBot)
e/ou Telegram. Feito para rodar de graça no GitHub Actions ou num celular
Android (Termux).

Uso:
  python monitor.py              -> roda uma vez (modo normal, usado no GitHub Actions)
  python monitor.py --teste      -> só mostra o que encontrou; não salva nem notifica
  python monitor.py --loop 5     -> roda para sempre, a cada 5 minutos (Termux/PC)
  python monitor.py --testar-whatsapp -> manda uma mensagem de teste

Variáveis de ambiente (no GitHub ficam em Settings > Secrets):
  CALLMEBOT_PHONE   número com DDI, ex: +5569999999999
  CALLMEBOT_APIKEY  chave recebida do CallMeBot
  GREENAPI_ID       idInstance da Green-API (alternativa ao CallMeBot)
  GREENAPI_TOKEN    apiTokenInstance da Green-API
  GREENAPI_DESTINO  número que recebe, com DDI, ex: 5569999999999
  GREENAPI_URL      (opcional) apiUrl mostrada no console, ex: https://7103.api.greenapi.com
  TELEGRAM_TOKEN    (opcional) token do bot do Telegram
  TELEGRAM_CHAT_ID  (opcional) chat id do Telegram
  FB_COOKIES        (opcional) cookies de uma conta secundária, ex: "c_user=...; xs=...; datr=..."
"""

import argparse
import json
import os
import random
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

PASTA = Path(__file__).resolve().parent
ARQ_CONFIG = PASTA / "config.json"
ARQ_ESTADO = PASTA / "estado.json"

MAX_VISTOS = 5000                 # quantos IDs guardar no histórico
FALHAS_PARA_AVISAR = 6            # execuções seguidas sem resultado antes de avisar
MAX_NOTIFICACOES_POR_RODADA = 15  # trava contra enxurrada de mensagens

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #
def normalizar(texto: str) -> str:
    """minúsculas, sem acento, espaços simples."""
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", texto.lower()).strip()


def log(msg: str) -> None:
    agora = datetime.now().strftime("%H:%M:%S")
    print(f"[{agora}] {msg}", flush=True)


def carregar_json(caminho: Path, padrao):
    if caminho.exists():
        with open(caminho, encoding="utf-8") as f:
            return json.load(f)
    return padrao


def salvar_estado(estado: dict) -> None:
    # mantém só os IDs mais recentes
    vistos = estado["vistos"]
    if len(vistos) > MAX_VISTOS:
        ordenados = sorted(vistos.items(), key=lambda kv: kv[1], reverse=True)
        estado["vistos"] = dict(ordenados[:MAX_VISTOS])
    with open(ARQ_ESTADO, "w", encoding="utf-8") as f:
        json.dump(estado, f, ensure_ascii=False, indent=1, sort_keys=True)


def estado_inicial() -> dict:
    return {
        "vistos": {},
        "buscas_inicializadas": [],
        "falhas_seguidas": 0,
        "aviso_bloqueio_enviado": False,
    }


# --------------------------------------------------------------------------- #
# Coleta no Facebook
# --------------------------------------------------------------------------- #
def montar_url(local: str, termo: str, raio_km: int) -> str:
    return (
        f"https://www.facebook.com/marketplace/{local}/search/"
        f"?query={quote(termo)}&sortBy=creation_time_descend"
        f"&radius={raio_km}&exact=false"
    )


def _percorrer(obj, achados: dict) -> None:
    """Procura, em qualquer profundidade, objetos que pareçam um anúncio."""
    if isinstance(obj, dict):
        if "marketplace_listing_title" in obj and obj.get("id"):
            achados[str(obj["id"])] = obj
        for v in obj.values():
            _percorrer(v, achados)
    elif isinstance(obj, list):
        for v in obj:
            _percorrer(v, achados)


def extrair_anuncios(html: str) -> list[dict]:
    """Lê os blocos JSON embutidos na página e devolve anúncios simplificados."""
    achados: dict = {}
    blocos = re.findall(
        r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', html, re.S
    )
    for bloco in blocos:
        if "marketplace_listing_title" not in bloco:
            continue
        try:
            _percorrer(json.loads(bloco), achados)
        except json.JSONDecodeError:
            continue

    anuncios = []
    for id_, n in achados.items():
        if n.get("is_sold") or n.get("is_hidden"):
            continue
        preco = n.get("listing_price") or {}
        local = ((n.get("location") or {}).get("reverse_geocode") or {})
        foto = ((n.get("primary_listing_photo") or {}).get("image") or {}).get("uri")
        valor = preco.get("amount")
        try:
            valor = float(valor) if valor is not None else None
        except (TypeError, ValueError):
            valor = None
        anuncios.append({
            "id": id_,
            "titulo": n.get("marketplace_listing_title", "").strip(),
            "preco_txt": preco.get("formatted_amount") or "sem preço",
            "preco": valor,
            "cidade": local.get("city") or "",
            "foto": foto,
            "link": f"https://www.facebook.com/marketplace/item/{id_}/",
        })
    return anuncios


def parece_bloqueado(html: str, url_final: str) -> bool:
    sinais = ["/login/", "login_form", "checkpoint", "id=\"loginbutton\""]
    return any(s in url_final for s in sinais[:1]) or any(s in html for s in sinais[1:])


def buscar(sessao: requests.Session, url: str) -> tuple[list[dict], bool]:
    """Retorna (anúncios, bloqueado?)."""
    try:
        r = sessao.get(url, headers=HEADERS, timeout=30, allow_redirects=True)
    except requests.RequestException as e:
        log(f"  erro de rede: {e}")
        return [], False
    anuncios = extrair_anuncios(r.text)
    bloqueado = not anuncios and (r.status_code != 200 or parece_bloqueado(r.text, r.url))
    if not anuncios:
        log(f"  status {r.status_code}, url final {r.url[:90]}, bloqueio={bloqueado}")
    return anuncios, bloqueado


# --------------------------------------------------------------------------- #
# Filtros
# --------------------------------------------------------------------------- #
def passa_filtro(anuncio: dict, busca: dict, excluir: list[str]) -> bool:
    titulo = normalizar(anuncio["titulo"])
    if not any(normalizar(p) in titulo for p in busca.get("titulo_deve_conter", [])):
        return False
    if any(normalizar(p) in titulo for p in excluir):
        return False
    pmax = busca.get("preco_max")
    if pmax is not None and anuncio["preco"] is not None and anuncio["preco"] > pmax:
        return False
    return True


# --------------------------------------------------------------------------- #
# Notificações
# --------------------------------------------------------------------------- #
def enviar(texto: str) -> bool:
    ok = False
    phone, apikey = os.getenv("CALLMEBOT_PHONE"), os.getenv("CALLMEBOT_APIKEY")
    if phone and apikey:
        try:
            r = requests.get(
                "https://api.callmebot.com/whatsapp.php",
                params={"phone": phone, "text": texto, "apikey": apikey},
                timeout=30,
            )
            ok = r.status_code == 200
            if not ok:
                log(f"  CallMeBot respondeu {r.status_code}: {r.text[:120]}")
        except requests.RequestException as e:
            log(f"  erro CallMeBot: {e}")
        time.sleep(3)  # CallMeBot não gosta de rajadas

    g_id, g_token, g_dest = os.getenv("GREENAPI_ID"), os.getenv("GREENAPI_TOKEN"), os.getenv("GREENAPI_DESTINO")
    if g_id and g_token and g_dest:
        base = (os.getenv("GREENAPI_URL") or "https://api.green-api.com").rstrip("/")
        destino = re.sub(r"\D", "", g_dest)
        try:
            r = requests.post(
                f"{base}/waInstance{g_id}/sendMessage/{g_token}",
                json={"chatId": f"{destino}@c.us", "message": texto},
                timeout=30,
            )
            ok = ok or r.status_code == 200
            if r.status_code != 200:
                log(f"  Green-API respondeu {r.status_code}: {r.text[:150]}")
        except requests.RequestException as e:
            log(f"  erro Green-API: {e}")
        time.sleep(2)

    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat, "text": texto},
                timeout=30,
            )
            ok = ok or r.status_code == 200
        except requests.RequestException as e:
            log(f"  erro Telegram: {e}")

    if not (phone and apikey) and not (token and chat) and not (g_id and g_token and g_dest):
        log("  (nenhum canal configurado — mensagem só no log)")
        print(texto)
    return ok


def formatar(anuncio: dict, nome_busca: str) -> str:
    linhas = [
        f"🔔 {nome_busca} no Marketplace",
        anuncio["titulo"],
        f"💰 {anuncio['preco_txt']}",
    ]
    if anuncio["cidade"]:
        linhas.append(f"📍 {anuncio['cidade']}")
    linhas.append(anuncio["link"])
    return "\n".join(linhas)


# --------------------------------------------------------------------------- #
# Rodada principal
# --------------------------------------------------------------------------- #
def rodar(teste: bool = False) -> None:
    config = carregar_json(ARQ_CONFIG, None)
    if not config:
        sys.exit("config.json não encontrado")
    estado = carregar_json(ARQ_ESTADO, estado_inicial())
    for k, v in estado_inicial().items():
        estado.setdefault(k, v)

    sessao = requests.Session()
    cookies = os.getenv("FB_COOKIES", "").strip()
    if cookies:
        for par in cookies.split(";"):
            if "=" in par:
                k, v = par.strip().split("=", 1)
                sessao.cookies.set(k, v, domain=".facebook.com")
        log("usando cookies de conta (FB_COOKIES)")

    agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
    total_lidos, algum_bloqueio, a_notificar = 0, False, []

    for busca in config["buscas"]:
        nome = busca["nome"]
        encontrados: dict = {}
        lidos_busca = 0
        for termo in busca["termos_busca"]:
            url = montar_url(config["local"], termo, config.get("raio_km", 10))
            log(f"buscando '{termo}'")
            anuncios, bloqueado = buscar(sessao, url)
            algum_bloqueio |= bloqueado
            lidos_busca += len(anuncios)
            total_lidos += len(anuncios)
            for a in anuncios:
                if passa_filtro(a, busca, config.get("palavras_excluir", [])):
                    encontrados[a["id"]] = a
            time.sleep(random.uniform(2, 5))

        novos = [a for a in encontrados.values() if a["id"] not in estado["vistos"]]
        log(f"{nome}: {len(encontrados)} relevantes, {len(novos)} novos")

        if teste:
            for a in encontrados.values():
                marca = "NOVO " if a in novos else "     "
                print(f"   {marca}{a['preco_txt']:>12}  {a['titulo'][:60]}  {a['link']}")
            continue

        primeira_vez = nome not in estado["buscas_inicializadas"]
        for a in novos:
            estado["vistos"][a["id"]] = agora
            if not primeira_vez:
                a_notificar.append((a, nome))
        if primeira_vez and lidos_busca:
            estado["buscas_inicializadas"].append(nome)
            log(f"  primeira execução de '{nome}': {len(novos)} anúncios antigos marcados como vistos")

    if teste:
        log(f"modo teste: {total_lidos} anúncios lidos no total. Nada foi salvo/enviado.")
        if total_lidos == 0:
            log("NENHUM anúncio lido — provável tela de login/bloqueio. Veja o README (FB_COOKIES).")
        return

    # notificações
    for a, nome in a_notificar[:MAX_NOTIFICACOES_POR_RODADA]:
        enviar(formatar(a, nome))
    if len(a_notificar) > MAX_NOTIFICACOES_POR_RODADA:
        enviar(f"(+{len(a_notificar) - MAX_NOTIFICACOES_POR_RODADA} anúncios novos não enviados nesta rodada)")

    # saúde do monitor
    if total_lidos == 0:
        estado["falhas_seguidas"] += 1
        log(f"nenhum anúncio lido ({estado['falhas_seguidas']} vezes seguidas)")
        if estado["falhas_seguidas"] >= FALHAS_PARA_AVISAR and not estado["aviso_bloqueio_enviado"]:
            motivo = "tela de login/bloqueio" if algum_bloqueio else "página mudou ou erro de rede"
            enviar(f"⚠️ Monitor do Marketplace não está conseguindo ler anúncios ({motivo}). Precisa de ajuste.")
            estado["aviso_bloqueio_enviado"] = True
    else:
        if estado["aviso_bloqueio_enviado"]:
            enviar("✅ Monitor do Marketplace voltou a funcionar.")
        estado["falhas_seguidas"] = 0
        estado["aviso_bloqueio_enviado"] = False

    salvar_estado(estado)
    log(f"fim: {total_lidos} lidos, {len(a_notificar)} notificados")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--teste", action="store_true", help="não salva nem notifica")
    ap.add_argument("--loop", type=float, metavar="MIN", help="repete a cada MIN minutos")
    ap.add_argument("--testar-whatsapp", action="store_true", help="manda mensagem de teste (WhatsApp/Telegram)")
    ap.add_argument("--descobrir-chat-id", action="store_true", help="mostra o chat id de quem falou com o bot")
    args = ap.parse_args()

    if args.descobrir_chat_id:
        token = os.getenv("TELEGRAM_TOKEN") or input("Token do bot: ").strip()
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30).json()
        chats = {}
        for u in r.get("result", []):
            msg = u.get("message") or u.get("my_chat_member") or {}
            chat = msg.get("chat") or {}
            if chat.get("id"):
                nome = chat.get("first_name") or chat.get("title") or ""
                chats[chat["id"]] = f"{nome} @{chat.get('username', '')}"
        if not chats:
            print("Nenhuma conversa ainda. Peça para a pessoa abrir o bot, tocar em Iniciar/Start e mandar 'oi'.")
        for cid, nome in chats.items():
            print(f"TELEGRAM_CHAT_ID = {cid}   ({nome})")
        return
    if args.testar_whatsapp:
        print("enviado" if enviar("✅ Teste do monitor do Marketplace") else "falhou")
        return
    if args.loop:
        while True:
            try:
                rodar(teste=args.teste)
            except Exception as e:  # mantém o loop vivo
                log(f"erro inesperado: {e}")
            espera = args.loop * 60 + random.uniform(-30, 30)
            time.sleep(max(60, espera))
    else:
        rodar(teste=args.teste)


if __name__ == "__main__":
    main()
