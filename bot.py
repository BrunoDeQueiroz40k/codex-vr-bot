"""
Bot de Discord que monitora um ou vários canais e, sempre que alguém
posta um link de avatar do VRChat, guarda o link + o título do avatar
em um arquivo JSON que pode ser lido por um frontend.

Também aceita comandos para adicionar/corrigir avatares manualmente,
útil quando o avatar não tem um nome bom para o filtro funcionar.

Comandos (prefixo padrão "!"):
    !add <link> <nome>          adiciona manualmente com o nome que você escolher
    !nome <link|id> <novo nome> corrige o nome de um avatar já registrado
    !remover <link|id>          remove um avatar do JSON

Como rodar:
    1. pip install -r requirements.txt
    2. copie .env.example para .env e coloque seu DISCORD_TOKEN
    3. preencha os IDs dos canais em config.json (ou deixe vazio para ouvir todos)
    4. python bot.py
"""

import asyncio
import json
import os
import re
import typing
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")
TOKEN = os.getenv("DISCORD_TOKEN")
with open(BASE_DIR / "config.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)
CHANNEL_IDS = set(int(c) for c in CONFIG.get("channel_ids", []))
OUTPUT_FILE = BASE_DIR / CONFIG.get("output_file", "avatars.json")
COMMAND_PREFIX = CONFIG.get("command_prefix", "!")
BLOB_TOKEN = os.getenv("BLOB_READ_WRITE_TOKEN")
BLOB_PATHNAME = CONFIG.get("blob_pathname", "avatars.json")
BLOB_CACHE_MAX_AGE = str(CONFIG.get("blob_cache_max_age", 60))
AVATAR_RE = re.compile(
    r"https?://(?:www\.)?vrchat\.com/home/avatar/(avtr_[0-9a-fA-F-]+)",
    re.IGNORECASE,
)
AVATAR_ID_RE = re.compile(r"(avtr_[0-9a-fA-F-]+)", re.IGNORECASE)
TITLE_TAG_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
META_CONTENT_RE = re.compile(r'content=["\']([^"\']*)["\']', re.IGNORECASE)

def _og_meta(html: str, key: str) -> str:
    """Lê o content de uma <meta og:KEY>, aceitando property= ou name= em
    qualquer ordem de atributos. Retorna string vazia se não achar."""
    tag_re = re.compile(
        r'<meta\b[^>]*?\b(?:property|name)=["\']og:' + re.escape(key) + r'["\'][^>]*>',
        re.IGNORECASE,
    )
    for tag in tag_re.finditer(html):
        m = META_CONTENT_RE.search(tag.group(0))
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()
    return ""

def _parse_name_creator(html: str) -> tuple[str, str]:
    """Separa o nome do avatar e o criador a partir dos metadados da VRChat.

    A VRChat expõe og:image:alt como:
        A preview image of VRChat avatar "NOME" by CRIADOR
    e og:title como:
        NOME by CRIADOR
    Retorna (nome, criador); criador pode vir vazio se não der pra separar.
    """
    alt = _og_meta(html, "image:alt")
    m = re.search(r'avatar\s+"(.+?)"\s+by\s+(.+?)\s*$', alt, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    title = _og_meta(html, "title")
    m = re.search(r"^(.*\S)\s+by\s+(.+?)\s*$", title, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return title, ""

# Lock para evitar que duas operações escrevam no JSON ao mesmo tempo.
file_lock = asyncio.Lock()

# ---------------------------------------------------------------------------
# Funções auxiliares
# ---------------------------------------------------------------------------

def load_avatars() -> list:
    """Carrega a lista atual de avatares do JSON (ou lista vazia)."""
    if OUTPUT_FILE.exists():
        try:
            with open(OUTPUT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except (json.JSONDecodeError, OSError):
            pass
    return []

def save_avatars(avatars: list) -> None:
    """Grava o JSON de forma atômica (escreve em arquivo temporário e renomeia)."""
    tmp = OUTPUT_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(avatars, f, ensure_ascii=False, indent=2)
    tmp.replace(OUTPUT_FILE)

async def publish_to_blob(avatars: list) -> None:
    """Sobe o avatars.json para o Vercel Blob, sobrescrevendo sempre o mesmo
    caminho (URL pública fixa). Só roda se BLOB_READ_WRITE_TOKEN existir; falhas
    são apenas logadas (os dados já foram salvos localmente)."""
    if not BLOB_TOKEN or bot.http_session is None:
        return
    payload = json.dumps(avatars, ensure_ascii=False, indent=2).encode("utf-8")
    url = f"https://blob.vercel-storage.com/{BLOB_PATHNAME}"
    headers = {
        "authorization": f"Bearer {BLOB_TOKEN}",
        "x-api-version": "7",
        "x-content-type": "application/json",
        "x-add-random-suffix": "0",   # mantém a URL pública estável
        "x-allow-overwrite": "1",     # permite sobrescrever o mesmo caminho
        "x-cache-control-max-age": BLOB_CACHE_MAX_AGE,
    }
    try:
        async with bot.http_session.put(
            url, data=payload, headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            body = await resp.text()
            if resp.status >= 300:
                print(f"[blob] falha ao publicar ({resp.status}): {body}")
                return
            info = json.loads(body)
            print(f"[blob] avatars.json publicado em: {info.get('url')}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"[blob] erro de rede ao publicar: {e}")

def extract_avatar_id(text: str) -> str | None:
    """Extrai o id (avtr_...) de um link OU de um id puro."""
    m = AVATAR_ID_RE.search(text)
    return m.group(1) if m else None

def is_monitored_channel(channel) -> bool:
    """Diz se devemos ouvir esse canal.

    Aceita o próprio canal e também tópicos (threads): um tópico tem
    parent_id apontando para o canal/fórum onde foi criado, então se o
    canal pai estiver na lista monitorada, o tópico também vale.
    Com CHANNEL_IDS vazio, ouve tudo.
    """
    if not CHANNEL_IDS:
        return True
    if channel.id in CHANNEL_IDS:
        return True
    parent_id = getattr(channel, "parent_id", None)
    return parent_id is not None and parent_id in CHANNEL_IDS

def channel_info(channel) -> dict:
    """Resolve a estrutura 'categoria > canal' onde a mensagem foi postada,
    para o site montar a sidebar igual ao Discord.

    Tópicos (threads) NÃO viram um elemento próprio: a mensagem é atribuída
    ao canal pai (que vive numa categoria). Guarda também as posições do
    canal e da categoria para ordenar igual ao Discord.
    """
    is_thread = isinstance(channel, discord.Thread)
    text_channel = getattr(channel, "parent", None) if is_thread else channel
    if text_channel is None:  # tópico sem pai no cache: cai pro próprio canal
        text_channel = channel

    category = getattr(text_channel, "category", None)

    return {
        "channel": getattr(text_channel, "name", str(getattr(text_channel, "id", ""))),
        "channel_id": getattr(text_channel, "id", None),
        "channel_position": getattr(text_channel, "position", None),
        "category": getattr(category, "name", None),
        "category_id": getattr(category, "id", None),
        "category_position": getattr(category, "position", None),
    }

async def fetch_metadata(session: aiohttp.ClientSession, url: str) -> dict:
    """Busca título, imagem e criador do avatar a partir dos metadados
    Open Graph da página. Retorna dict com chaves title/image/creator
    (strings vazias quando não encontrado)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )
    }
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            html = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return {"title": "", "image": "", "creator": ""}

    title, creator = _parse_name_creator(html)
    if not title:
        m = TITLE_TAG_RE.search(html)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()
    # A página do VRChat costuma vir como "Nome do avatar - VRChat".
    title = re.sub(r"\s*[-|]\s*VRChat\s*$", "", title, flags=re.IGNORECASE)

    return {
        "title": title,
        "image": _og_meta(html, "image"),
        "creator": creator,
    }

async def rename_avatar(avatar_id: str, new_title: str) -> bool:
    """Atualiza o título de um avatar existente. Retorna True se achou."""
    async with file_lock:
        avatars = load_avatars()
        found = False
        for a in avatars:
            if a.get("id") == avatar_id:
                a["title"] = new_title
                a["title_manual"] = True
                found = True
        if found:
            save_avatars(avatars)
    if found:
        await publish_to_blob(avatars)
    return found

async def remove_avatar(avatar_id: str) -> bool:
    """Remove um avatar do JSON. Retorna True se removeu algo."""
    async with file_lock:
        avatars = load_avatars()
        new_list = [a for a in avatars if a.get("id") != avatar_id]
        if len(new_list) == len(avatars):
            return False
        save_avatars(new_list)
    await publish_to_blob(new_list)
    return True

async def add_many(entries: list) -> int:
    """Adiciona vários avatares de uma vez, pulando os que já existem. Salva e
    publica no Blob uma única vez. Retorna quantos foram realmente adicionados."""
    if not entries:
        return 0
    async with file_lock:
        avatars = load_avatars()
        have = {a.get("id") for a in avatars}
        added = 0
        for e in entries:
            if e["id"] in have:
                continue
            avatars.append(e)
            have.add(e["id"])
            added += 1
        if added:
            save_avatars(avatars)
    if added:
        await publish_to_blob(avatars)
    return added

# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True  # privilegiado: precisa estar ligado no Dev Portal

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)
bot.http_session = None  # criada no setup_hook

@bot.event
async def setup_hook():
    bot.http_session = aiohttp.ClientSession()

@bot.event
async def on_ready():
    print(f"Bot conectado como {bot.user} (id: {bot.user.id})")
    if CHANNEL_IDS:
        print(f"Monitorando {len(CHANNEL_IDS)} canal(is): {sorted(CHANNEL_IDS)}")
    else:
        print("Monitorando TODOS os canais visíveis (config.channel_ids vazio).")
    print(f"Prefixo de comandos: {COMMAND_PREFIX}")
    if BLOB_TOKEN:
        # Publica o estado atual e mostra a URL pública (cole-a no site).
        await publish_to_blob(load_avatars())
    else:
        print("[blob] BLOB_READ_WRITE_TOKEN não definido — publicando só localmente.")

@bot.event
async def on_message(message: discord.Message):
    # Ignora as próprias mensagens do bot.
    if message.author == bot.user:
        return

    # Comandos (começam com o prefixo) são tratados pelo handler de comandos.
    if message.content.startswith(COMMAND_PREFIX):
        await bot.process_commands(message)
        return

    # Detecção automática: nos canais configurados (ou todos, se vazio).
    # Também vale para tópicos cujo canal pai está monitorado.
    if not is_monitored_channel(message.channel):
        return

    for m in AVATAR_RE.finditer(message.content):
        await handle_auto_avatar(message, m.group(0), m.group(1))

async def build_avatar_entry(message: discord.Message, url: str, avatar_id: str) -> dict:
    """Monta o registro de um avatar a partir da mensagem onde o link apareceu
    (busca metadados na VRChat e resolve categoria/canal)."""
    meta = await fetch_metadata(bot.http_session, url)
    return {
        "id": avatar_id,
        "url": url,
        "title": meta["title"] or "(título não encontrado)",
        "title_manual": False,
        "image": meta["image"],
        "creator": meta["creator"],
        "posted_by": str(message.author),
        **channel_info(message.channel),
        "message_id": message.id,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }

async def handle_auto_avatar(message: discord.Message, url: str, avatar_id: str):
    """Trata um link detectado automaticamente em uma mensagem comum."""
    entry = await build_avatar_entry(message, url, avatar_id)
    if await add_many([entry]):
        print(f"+ Avatar adicionado: {entry['title']} ({url})")
        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass
    else:
        print(f"Avatar já registrado, ignorando: {avatar_id}")


# --------------------------- Comandos --------------------------------------

@bot.command(name="add")
async def cmd_add(ctx: commands.Context, link: str, *, nome: str):
    """!add <link> <nome> — adiciona um avatar manualmente com o nome escolhido."""
    avatar_id = extract_avatar_id(link)
    if not avatar_id:
        await ctx.reply("❌ Não encontrei um id de avatar (avtr_...) nesse link.")
        return

    # Mantém o nome escolhido, mas ainda busca imagem/criador e a categoria/canal.
    url = f"https://vrchat.com/home/avatar/{avatar_id}"
    entry = await build_avatar_entry(ctx.message, url, avatar_id)
    entry["title"] = nome.strip()
    entry["title_manual"] = True
    if await add_many([entry]):
        await ctx.reply(f"✅ Adicionado: **{entry['title']}**")
    else:
        await ctx.reply(
            f"⚠️ Esse avatar já existe. Use `{COMMAND_PREFIX}nome {avatar_id} <novo nome>` "
            "para corrigir o nome."
        )

@bot.command(name="nome", aliases=["rename", "renomear"])
async def cmd_nome(ctx: commands.Context, link: str, *, novo_nome: str):
    """!nome <link|id> <novo nome> — corrige o nome de um avatar já registrado."""
    avatar_id = extract_avatar_id(link)
    if not avatar_id:
        await ctx.reply("❌ Não encontrei um id de avatar (avtr_...) nisso.")
        return
    if await rename_avatar(avatar_id, novo_nome.strip()):
        await ctx.reply(f"✏️ Nome atualizado para **{novo_nome.strip()}**")
    else:
        await ctx.reply(
            f"❌ Não achei esse avatar no JSON. Use `{COMMAND_PREFIX}add <link> <nome>` "
            "para adicionar."
        )

@bot.command(name="remover", aliases=["remove", "del"])
async def cmd_remover(ctx: commands.Context, link: str):
    """!remover <link|id> — remove um avatar do JSON."""
    avatar_id = extract_avatar_id(link)
    if not avatar_id:
        await ctx.reply("❌ Não encontrei um id de avatar (avtr_...) nisso.")
        return
    if await remove_avatar(avatar_id):
        await ctx.reply(f"🗑️ Removido: `{avatar_id}`")
    else:
        await ctx.reply("❌ Esse avatar não está no JSON.")

@bot.command(name="scan", aliases=["escanear", "varrer"])
async def cmd_scan(
    ctx: commands.Context,
    channel: typing.Optional[typing.Union[discord.TextChannel, discord.Thread]] = None,
    limit: typing.Optional[int] = None,
):
    """!scan [#canal|tópico] [limite] — lê o histórico e cadastra os links que
    ainda não estão no JSON (os já existentes são ignorados).

    Sem argumentos, escaneia o canal/tópico atual e todo o histórico.
    Ex.: `!scan`, `!scan 500`, `!scan #ultramarine`, `!scan #ultramarine 1000`.
    """
    target = channel or ctx.channel

    # Ids já cadastrados, pra pular o fetch de quem já existe.
    async with file_lock:
        existing = {a.get("id") for a in load_avatars()}

    status = await ctx.reply(f"🔍 Escaneando **#{target}** — buscando links de avatar…")

    scanned = 0       # mensagens lidas
    found = 0         # links encontrados (com repetição)
    skipped = 0       # já existiam ou repetidos no próprio scan
    seen = set()      # ids vistos neste scan
    new_entries = []

    try:
        async for message in target.history(limit=limit, oldest_first=True):
            scanned += 1
            for m in AVATAR_RE.finditer(message.content):
                url, avatar_id = m.group(0), m.group(1)
                found += 1
                if avatar_id in existing or avatar_id in seen:
                    skipped += 1
                    continue
                seen.add(avatar_id)
                new_entries.append(await build_avatar_entry(message, url, avatar_id))
            if scanned % 200 == 0:
                await status.edit(
                    content=f"🔍 Escaneando **#{target}**… {scanned} mensagens, "
                    f"{len(new_entries)} novos encontrados."
                )
    except discord.Forbidden:
        await status.edit(content=f"❌ Sem permissão para ler o histórico de **#{target}**.")
        return

    added = await add_many(new_entries)
    await status.edit(
        content=(
            f"✅ Scan de **#{target}** concluído.\n"
            f"• {scanned} mensagens lidas\n"
            f"• {found} link(s) de avatar encontrados\n"
            f"• **{added} novo(s)** adicionado(s)\n"
            f"• {skipped} já existiam / repetidos"
        )
    )

@bot.command(name="ajuda", aliases=["help", "comandos"])
async def cmd_ajuda(ctx: commands.Context):
    p = COMMAND_PREFIX
    await ctx.reply(
        "**Comandos:**\n"
        f"`{p}add <link> <nome>` — adiciona manualmente com o nome escolhido\n"
        f"`{p}nome <link|id> <novo nome>` — corrige o nome de um avatar existente\n"
        f"`{p}remover <link|id>` — remove um avatar do JSON\n"
        f"`{p}scan [#canal|tópico] [limite]` — varre o histórico e cadastra os links novos\n"
        "Links postados normalmente nos canais monitorados são adicionados sozinhos."
    )

@cmd_add.error
@cmd_nome.error
async def arg_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"❌ Faltou argumento. Veja `{COMMAND_PREFIX}ajuda`.")
    else:
        raise error

def main():
    if not TOKEN:
        raise SystemExit(
            "DISCORD_TOKEN não encontrado. Copie .env.example para .env e preencha o token."
        )
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
