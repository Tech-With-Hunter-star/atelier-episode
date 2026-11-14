# Atelier d'épisode

一集一集啃法语播客的学习 app。任何音频都可以：有 transcript 最好，没有也能用。
所有东西都跑在你自己的电脑上，不需要云服务器。

```
音频 (+ transcript) → import_episode.py → episode.json ─┐
                      generate_images.py → images/ ─────┤
                      generate_voice.py  → voice/  ─────┼→ server.py → 手机/电脑上的 app
            (可选) db.py → PostgreSQL：集数内容 + 你的进度 ┘
```

一条命令做完一集的全部步骤：`python prepare.py episodes/你的文件夹`。

## 文件夹里有什么

| 路径 | 作用 |
| --- | --- |
| `server.py` | 服务器：把 app、集数、音频、图片发给浏览器，保存进度，接收从 app 里新增的集数 |
| `import_episode.py` | 导入脚本：听音频、给每一句配上时间、让 Claude 选词并写图片描述，输出 `episode.json` |
| `generate_images.py` | 用本地图片模型给每个词画一张图（还没在真实模型上跑过） |
| `generate_voice.py` | 给原音频里剪不出来的单词、以及生成的例句配上合成语音（macOS 的 `say` 还没在你的电脑上跑过） |
| `prepare.py` | 一条命令：导入 → 图片 → 语音 → 数据库，缺哪一步的工具就跳过并告诉你 |
| `db.py`、`schema.sql` | PostgreSQL：建表、把 `episode.json` 装进表里、保存进度 |
| `docker-compose.yml` | 在 Docker 里启动一个 PostgreSQL |
| `tests/` | 自动测试（`pytest`） |
| `app/index.html` | app 本身 |
| `episodes/` | 每集一个子文件夹：音频、transcript、`episode.json`、`images/` |
| `data/store.json` | 没有数据库时，你的进度和设置（服务器第一次保存时自动创建） |
| `requirements.txt` | 服务器、导入脚本、数据库需要的包 |
| `requirements-dev.txt` | 跑测试需要的包 |
| `requirements-images.txt` | 生成图片需要的包（很大，单独装） |
| `_backup/` | 每次 Claude 改动现有文件前留的旧版本 |

## 第一次使用

```
cd ~/dev/atelier-episode
conda activate atelier
pip install -r requirements.txt
```

Claude 的登录方式二选一：已经运行过 `ant auth login` 就不用做别的；
或者 `cp .env.example .env` 然后把 API key 填进去。

## 启动 app

```
python server.py
```

它会打印两个地址：

- 电脑上：`http://localhost:8000`
- 手机上（连同一个 Wi-Fi）：`http://你的电脑地址:8000`

手机上用浏览器打开后，选「添加到主屏幕」，它就像一个普通 app 一样有图标、全屏打开。
第一次启动时 macOS 可能会问是否允许 Python 接受网络连接，选允许，否则手机连不上。

停止：在终端按 Ctrl+C。电脑关机或服务器停了，手机上就打不开。

## 新增一集

**方法一：在 app 里**

打开 Ajouter，选音频文件，有 transcript 就贴进去，点「Envoyer à mon ordinateur」。
电脑会在后台听音频、切句子、选词，app 上显示进度，好了自动出现。30 分钟的音频大约要 15 分钟。

**方法二：在终端里**

1. 在 `episodes/` 下建一个子文件夹，名字随意（只用字母、数字和连字符）。
2. 放进一个音频文件；有 transcript 就再放一个 `.txt`。
3. 运行 `python import_episode.py episodes/你的文件夹`，然后刷新 app。

### 文字从哪里来

| 情况 | 脚本怎么做 |
| --- | --- |
| 有 transcript，而且和音频对得上 | 用 transcript 的文字，时间精确到每一句 |
| 没有 transcript | 用 Whisper 听出来的文字（会有少量错误） |
| 有 transcript，但和音频对不上 | 自动改用 Whisper 听出来的文字，并提示你 |

### 导入脚本的选项

| 选项 | 作用 |
| --- | --- |
| `--title "…"` | app 里显示的标题（默认取文件夹的名字） |
| `--level A1` | 你的水平，选词时使用（默认 A1） |
| `--max-words 0` | 最多选多少个词；`0`（默认）= 这个水平值得学的词全部选出来 |
| `--repeat-known` | 以前标记为「Je connais」的词也照样选（默认会跳过） |
| `--tag-only` | 不动词表，只让 Claude 给每个词标上词性（动词、名词、形容词…），一次很短的请求 |
| `--whisper medium` | 更准但更慢的语音模型（默认 `small`） |
| `--max-chars 220` | 超过这个长度的句子会在逗号或连接词处切开（`0` = 不切） |
| `--listen-again` | 不用上次的听写结果 `heard.json`，重新听一遍音频 |
| `--no-vocab` | 只做文字和时间，不调用 Claude（已经选好的词会保留） |
| `--words-only` | 不重新听音频，只让 Claude 重新选词和写图片描述 |

### 选词：从你的水平出发，认识的词不再出现

- Claude 把句子分成每 50 句一段，一段一段地选，所以词的数量不再限于 40 个。
- 在 app 的 Vocabulaire → Mots 里，每个词下面有「Je connais」。点了它（或者在 Jeu 里答对），这个词就算认识了。
- 准备**下一集**时，脚本会先查出其他集里所有认识的词，告诉 Claude 不要选，返回后代码再过滤一遍。
  有数据库时查 `word_progress` 表，没有时读 `data/store.json`。
- 只让 Claude 重新选词、不重新听音频：`python import_episode.py episodes/episode-01 --words-only`
  （会替换这一集现有的词表，这一集上已有的单词进度会对不上）。

### 11 月 13 日导入的那一集需要重新准备一次

那次的 `episode.json` 里没有每个单词的时间，也没有图片描述，所以单词剪不出原音。运行：

```
python import_episode.py episodes/episode-01 --title "French Mornings · Ep18 · Motivation 2025"
```

这一次要重新听音频（约 15 分钟），之后听写结果存在 `heard.json` 里，再运行就不用听了。
注意：长句子现在会被切开，句子的编号会变，这一集上已经存下的进度（星星、听写分数）会对不上，
建议在 app 的 Réglages 里把这一集清零。

## 声音：除了少数单词，听到的全是播客原音

规则：

- 「Phrase」播整句原音。
- 「Extrait」播原音里这个词前后的几个词（大约 3 到 4 秒）。播客的句子很长，这一小段是主持人的原声、原语速、原来的连读。
  句子本身很短（12 个词以内）时没有「Extrait」，直接听整句。
- 「Mot」：这一集里说的就是词典形式（"atteindre"）→ 播原音里剪出来的这个词，按钮上有 ●。
- 这一集里说的是变位或变形（词典形式 "souhaiter"，集里说 "souhaite"），或者这个词在音频里剪不准 → 由女声 Denise 读词典形式。
  这是整个 app 里唯一用到合成语音的地方。

句子从不交给合成语音去读，也不再让 Claude 另写例句：合成语音没有真人的语调和情绪。

```
pip install edge-tts
python generate_voice.py episodes/episode-01 --list     # 只看要生成什么
python generate_voice.py episodes/episode-01            # 生成全部，已有的文件会跳过
```

- `edge-tts` 把每个词发给微软的在线朗读服务，所以需要联网。个别失败的文件，重新运行同一条命令就会补上。
- 语音文件还没生成时，「Mot」照旧播原音，什么都不会坏。
- 离线的替代：`--engine say --voice Amélie`（Mac 自带的声音）。接别的程序：`--command "程序 … {out}"`。
- `voice/sources.json` 记录每个词的声音来源和原因；服务器只播放登记过「读的就是这个词」的文件。

## 生成图片

图片在你的电脑上用本地模型生成，不花钱。**这个脚本还没在真实模型上跑过**，第一次测试就是真正的检验。

```
pip install -r requirements-images.txt
python generate_images.py episodes/episode-01 --test      # 先出一张，会报告用了多少秒
python generate_images.py episodes/episode-01             # 满意后跑全部，已有的图会跳过
```

- 需要 `episode.json` 里有图片描述（见上面「需要重新准备一次」）。
- 默认模型是 `stabilityai/sd-turbo`，512 px，2 步。第一次会下载模型（几个 GB）。
- 生成时关掉浏览器标签页和其他占内存的 app。
- 图片存在 `episodes/那一集/images/`，刷新 app 就能在单词卡上看到。

## PostgreSQL（可选）

不设置也能用：服务器直接读文件夹，进度存在 `data/store.json`。设置之后，集数内容和进度都存在 PostgreSQL 里，
音频、图片、语音仍然是硬盘上的文件。

```
docker compose up -d                 # 启动 PostgreSQL（需要 Docker Desktop 在运行）；在电脑上的端口是 5433
cp .env.example .env                 # 如果还没有 .env；然后去掉 ATELIER_DB 那一行前面的 #
python db.py sync                    # 建表，把 episodes/ 里的每一集装进去，并把 data/store.json 的进度搬进来（只搬一次）
python server.py                     # 启动时会打印「Données : PostgreSQL (…)」
```

- 内容表：`episodes`、`sentences`、`sentence_words`、`vocab`。每次 `sync` 按 `episode.json` 重建，不会重复。
- 词典表 `lexicon`：一行一个法语词，全局唯一，`id` 就是这个词在所有集里的编号。`vocab` 里的每一行是这个词在某一集的一次出现，
  用 `lexicon_id` 指向它。`lexicon` 在重新装载时不会清空，所以编号永远不变。
- 学习者表：`users`、`user_store`（app 存的原样）、`sentence_progress`、`word_progress`、`activity_days`，每一行都有 `user_id`。
  `word_progress` 按 `(user_id, lexicon_id)` 记录：一个词在任何一集里标记为认识，就是全局认识。
- `user_words`：你自己从句子里点选加入的词（`vocab` 里是 Claude 选的）。重新导入一集不会动它；
  app 设置里「把这一集清零」会同时清掉进度和这一集里你加的词。
  重新导入一集不会删掉进度。
- 视图 `episode_progress`：每一集背了几句、听写平均分、认识几个词、今天该复习几个词。
- 数据库没启动时，服务器自动退回到读文件，并在启动时告诉你。
- `python db.py status` 看每张表有多少行。

## 测试

```
pip install -r requirements-dev.txt
pytest                                                          # 不需要 Whisper、Claude 或数据库
ATELIER_TEST_DB=postgresql://atelier:atelier@localhost:5433/atelier_test pytest   # 加上数据库测试
```

数据库测试会**清空** `ATELIER_TEST_DB` 指向的那个库，所以要用一个单独的库
（`docker compose exec db createdb -U atelier atelier_test`），不要指向你真正的库。

## 离线：电脑合上盖子也能在手机上学

默认情况下手机每次都向电脑要内容，电脑一睡眠 app 就用不了。可以把一集复制到手机里：

1. 手机要用 **https** 地址打开 app（浏览器不允许 http 的网页保存离线副本）。
   用 Tailscale（免费）：电脑和手机各装一个，登录同一个账号，在电脑上运行 `tailscale serve --bg 8000`，
   然后在手机上打开它打印出来的地址，重新"添加到主屏幕"。
2. 点顶部的下载按钮 → "Télécharger sur cet appareil"。音频、图片、单词和 app 本身都会存进手机。
3. 之后电脑合着也能学。进度先记在手机上，下次连上电脑时自动传回去；按钮上有小圆点表示还有进度没传。

规则：同一集如果两边都改了，以后改的那一方为准。离线时不能新增一集。
实现在 `app/sw.js`（service worker）：电脑不回应时，由它用手机里的副本回答。

## 出门在外用手机

服务器没有登录，所以不要把端口直接开到公网。安全的做法是 Tailscale：电脑和手机都装上并登录同一个账号，
然后在手机上打开 `http://电脑在 Tailscale 里的名字:8000`。电脑要开着并且 `server.py` 在运行。这一步还没试过。

## 数据存在哪里

| 数据 | 位置 |
| --- | --- |
| 句子、时间、单词 | `episodes/那一集/episode.json` |
| 音频、图片（`images/`）、合成语音（`voice/`）、听写缓存（`heard.json`） | 同一个文件夹 |
| 星星、掌握的句子、听写分数、复习盒子、设置 | `data/store.json`；设置了 `ATELIER_DB` 时在 PostgreSQL 里 |

通过服务器打开 app 时，进度存在电脑上，手机和电脑看到的是同一份。
直接双击 `app/index.html` 打开时，没有服务器，进度只存在那个浏览器里。

## 现在的限制

- 只支持法语音频。
- 只有一个学习者（`user_id = 1`）；表里已经为多人留好了位置，但 app 没有登录。
- 从 app 里新增一集只做导入；图片和语音要在终端里运行 `prepare.py` 或各自的脚本。
- 没有登录：同一个 Wi-Fi 上的人只要知道地址就能打开。只在自己家里的网络用。
- 出门后手机连不上电脑，除非用 Tailscale（见上）。
- 同一时间只能准备一集。

## 注意

- `.env` 和 `~/.config/anthropic/` 里的内容是你的登录凭证，不要提交到 git，也不要发给别人。
- 音频和 transcript 属于它们的作者，只留作个人学习用；`.gitignore` 已经把 `episodes/` 排除在外。
