# Bot de Avatares do VRChat

Bot de Discord que monitora um ou **vários canais ao mesmo tempo** (incluindo
**tópicos**). Sempre que alguém posta um link de avatar do VRChat
(`https://vrchat.com/home/avatar/avtr_...`), o bot lê os metadados Open Graph da
página e salva em `avatars.json`:

- **título** e **criador** do avatar (extraídos de `og:title` / `og:image:alt`);
- **imagem** de preview (`og:image`);
- **categoria** e **canal** do Discord onde o link foi postado (com as posições,
  para o site ordenar igual ao Discord).

Esse `avatars.json` é consumido pelo site em [`../codex-vr`](../codex-vr).

## Instalação

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuração

1. **Token:** copie `.env.example` para `.env` e coloque seu token:
   ```
   DISCORD_TOKEN=seu_token_aqui
   ```
   O token fica em https://discord.com/developers/applications → sua aplicação →
   **Bot** → *Reset Token*.

2. **Ativar a intent de mensagens:** no mesmo painel **Bot**, ligue
   **MESSAGE CONTENT INTENT** (obrigatório para o bot ler o conteúdo das mensagens).

3. **Canais (`config.json`):**
   ```json
   {
     "channel_ids": [123456789012345678, 987654321098765432],
     "output_file": "../codex-vr/avatars.json",
     "command_prefix": "!"
   }
   ```
   - `channel_ids`: IDs dos canais a monitorar. **Vazio (`[]`)** = ouve **todos**
     os canais visíveis. Para pegar o ID: ative *Modo Desenvolvedor* (Configurações
     → Avançado), botão direito no canal → *Copiar ID do canal*.
   - **Tópicos:** basta colocar o ID do **canal pai** — os tópicos dele são
     monitorados automaticamente. (Um avatar postado num tópico é atribuído ao
     canal pai, não ao tópico.)
   - `output_file`: onde gravar o JSON (por padrão, dentro do site).

4. **Convidar o bot:** em **OAuth2 → URL Generator**, marque `bot` e as permissões
   *View Channels*, *Read Message History* e *Add Reactions*.
   (*Read Message History* é necessária para o `!scan`.)

5. **(Opcional) Publicar na web — Vercel Blob:** para o site hospedado na Vercel ler
   sempre a versão mais recente, defina no `.env`:
   ```
   BLOB_READ_WRITE_TOKEN=vercel_blob_rw_...
   ```
   Pegue em Vercel → **Storage** → crie um **Blob Store** → copie a variável.
   Sem isso, o bot só grava o arquivo local. Veja o passo a passo completo no
   [README do site](../codex-vr/README.md#deploy-na-vercel-site--vercel-blob).

## Rodar

```bash
python bot.py
```

Quando um avatar for registrado automaticamente, o bot reage com ✅ na mensagem.
Se o `BLOB_READ_WRITE_TOKEN` estiver definido, no log do `on_ready` aparece a URL
pública do Blob (a que você cola no site).

> O bot é um processo **sempre-ligado** — rode num host que fique no ar (sua
> máquina, VPS, Railway, Fly.io…). Ele **não** roda na Vercel.

## Comandos

Prefixo padrão `!` (configurável em `config.json`):

| Comando | O que faz |
|---|---|
| `!scan [#canal\|tópico] [limite]` | Varre o histórico e cadastra os links **ainda não registrados** (ignora os que já existem). Sem argumentos, usa o canal/tópico atual e todo o histórico. |
| `!add <link> <nome>` | Adiciona manualmente já atribuindo o **nome** que você escolher (mantém o nome, mas ainda busca imagem/criador). |
| `!nome <link\|id> <novo nome>` | Corrige o **nome** de um avatar já registrado. |
| `!remover <link\|id>` | Remove um avatar do JSON. |
| `!ajuda` | Lista os comandos. |

Os comandos aceitam o link completo **ou** só o id (`avtr_...`). Avatares
adicionados/corrigidos manualmente ficam com `"title_manual": true` no JSON.

### Exemplos do `!scan`

```
!scan                       # canal/tópico atual, histórico inteiro
!scan 500                   # canal atual, últimas 500 mensagens
!scan #ultramarine          # aquele canal, histórico inteiro
!scan #ultramarine 1000     # aquele canal, últimas 1000 mensagens
```

Para escanear um tópico específico, rode `!scan` **dentro dele**. Ao final, o bot
mostra um resumo (mensagens lidas, links encontrados, novos adicionados, ignorados)
e publica o JSON uma única vez.

## Formato do `avatars.json`

```json
[
  {
    "id": "avtr_xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
    "url": "https://vrchat.com/home/avatar/avtr_...",
    "title": "Nome do avatar",
    "title_manual": false,
    "image": "https://api.vrchat.cloud/api/1/file/.../file",
    "creator": "NomeDoCriador",
    "posted_by": "usuario",
    "channel": "ultramarine",
    "channel_id": 123456789012345678,
    "channel_position": 45,
    "category": "CAPÍTULOS LEGALISTAS",
    "category_id": 1373321121037811742,
    "category_position": 17,
    "message_id": 111111111111111111,
    "added_at": "2026-06-17T15:52:25+00:00"
  }
]
```

- `image` / `creator` podem vir vazios se a página não trouxer os metadados.
- `category*` fica `null` se o canal não estiver dentro de uma categoria.
- Avatares duplicados (mesmo `id`) são ignorados automaticamente.
