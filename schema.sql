-- Atelier d'épisode — PostgreSQL tables.
-- Run by:  python db.py init      (safe to run again: nothing is dropped)
--
-- Two families of tables:
--   content   what an episode IS      (the same for everybody; rebuilt from episode.json by "python db.py sync")
--   learner   what a person DID       (every row carries a user_id; never rebuilt, only updated by the app)

-- ---------------------------------------------------------------- content

create table if not exists episodes (
    id           text primary key,                 -- the folder name in episodes/
    title        text        not null,
    level        text,
    audio_file   text,                             -- file name inside the episode folder
    duration_s   numeric(9,2) not null default 0,
    text_source  text,                             -- 'transcript' or 'whisper'
    loaded_at    timestamptz not null default now()
);

create table if not exists sentences (
    episode_id   text    not null references episodes(id) on delete cascade,
    idx          integer not null,                 -- position in the episode, from 0
    text         text    not null,
    new_para     boolean not null default false,
    start_s      numeric(9,2) not null,
    end_s        numeric(9,2) not null,
    primary key (episode_id, idx)
);

-- the time of every spoken word, used to cut single words out of the original audio
create table if not exists sentence_words (
    episode_id   text    not null,
    sentence_idx integer not null,
    idx          integer not null,                 -- position in the sentence, from 0
    word         text    not null,
    start_s      numeric(9,2) not null,
    end_s        numeric(9,2) not null,
    heard        boolean not null default true,    -- false: the time is an estimate, not safe to cut
    primary key (episode_id, sentence_idx, idx),
    foreign key (episode_id, sentence_idx) references sentences(episode_id, idx) on delete cascade
);

-- the dictionary: one row per French word, whatever the episode. Its id is the word's number everywhere.
-- Never emptied when an episode is reloaded, so a word keeps its id (and your progress on it) for good.
create table if not exists lexicon (
    id         integer generated always as identity primary key,
    lemma      text not null unique,               -- the word made comparable: lowercase, without its article ("défi")
    display    text not null,                      -- as shown to the learner ("un défi")
    pos        text,                               -- kind of word: verbe, nom, adjectif, adverbe, expression, autre
    created_at timestamptz not null default now()
);

-- one row per APPEARANCE of a word in an episode; lexicon_id says which word it is
create table if not exists vocab (
    episode_id   text    not null references episodes(id) on delete cascade,
    idx          integer not null,                 -- position in the word list, from 0 (also the image file name)
    lexicon_id   integer references lexicon(id),   -- the same id in every episode where this word appears
    word         text    not null,                 -- dictionary form: "un défi"
    form         text    not null,                 -- as spoken in the sentence: "défis"
    definition   text,
    sentence_idx integer not null,
    image_prompt text,
    gen_example  text,                             -- a short generated sentence, when the original one is long
    gen_form     text,
    clip_start_s numeric(9,2),                     -- where the word is spoken in the original audio (null: not found)
    clip_end_s   numeric(9,2),
    ctx_start_s  numeric(9,2),                     -- a few words of the original sentence around the word (null: the
    ctx_end_s    numeric(9,2),                     --   sentence is short enough to be heard whole)
    ctx_text     text,
    primary key (episode_id, idx),
    foreign key (episode_id, sentence_idx) references sentences(episode_id, idx) on delete cascade
);

alter table vocab add column if not exists lexicon_id integer references lexicon(id);   -- databases made before the dictionary
alter table lexicon add column if not exists pos text;
alter table vocab add column if not exists ctx_start_s numeric(9,2);
alter table vocab add column if not exists ctx_end_s numeric(9,2);
alter table vocab add column if not exists ctx_text text;
create index if not exists vocab_lexicon on vocab (lexicon_id);

-- ---------------------------------------------------------------- learner

create table if not exists users (
    id         integer primary key,
    name       text not null,
    created_at timestamptz not null default now()
);
insert into users (id, name) values (1, 'moi') on conflict (id) do nothing;

-- exactly what the app saves, one row per key. This is what the app reads back.
create table if not exists user_store (
    user_id    integer not null references users(id) on delete cascade,
    key        text    not null,
    value      jsonb,
    updated_at timestamptz not null default now(),
    primary key (user_id, key)
);

-- the three tables below are filled from user_store every time the app saves (and can always be rebuilt from it),
-- so that progress can be queried with ordinary SQL.
-- They do not reference the content tables on purpose: reloading an episode must never erase progress.

create table if not exists sentence_progress (
    user_id          integer not null references users(id) on delete cascade,
    episode_id       text    not null,
    sentence_idx     integer not null,
    recited          boolean not null default false,   -- learnt by heart
    recite_step      smallint,                         -- 1 reading, 2 hints, 3 hidden
    dictation_best   numeric(4,1),                     -- best mark out of 20
    dictation_passed boolean not null default false,
    updated_at       timestamptz not null default now(),
    primary key (user_id, episode_id, sentence_idx)
);

-- one row per learner and WORD (not per episode): knowing a word is true everywhere
create table if not exists word_progress (
    user_id     integer not null references users(id) on delete cascade,
    lexicon_id  integer not null references lexicon(id),
    known       boolean not null default false,        -- "Je connais", or passed in the game
    known_in    text[]  not null default '{}',         -- the episodes where it was marked known
    box         smallint,                              -- Leitner box 1..5 (the highest reached)
    due         date,                                  -- next review
    updated_at  timestamptz not null default now(),
    primary key (user_id, lexicon_id)
);

-- the words the learner added herself by touching a sentence (vocab holds the ones Claude chose).
-- Never touched when an episode is loaded again.
create table if not exists user_words (
    user_id      integer not null references users(id) on delete cascade,
    episode_id   text    not null,
    sentence_idx integer not null,                     -- the sentence she touched
    form         text    not null,                     -- the word as she picked it
    lexicon_id   integer not null references lexicon(id),
    added_at     timestamptz not null default now(),
    primary key (user_id, episode_id, sentence_idx, form)
);

create table if not exists activity_days (
    user_id integer not null references users(id) on delete cascade,
    day     date    not null,
    primary key (user_id, day)
);

-- one line per learner and episode: how far along
create or replace view episode_progress as
select u.id as user_id, e.id as episode_id, e.title,
       (select count(*) from sentences s where s.episode_id = e.id)                                   as sentences,
       (select count(*) from sentence_progress p where p.user_id = u.id and p.episode_id = e.id and p.recited)          as recited,
       (select count(*) from sentence_progress p where p.user_id = u.id and p.episode_id = e.id and p.dictation_passed) as dictations_passed,
       (select round(avg(p.dictation_best), 1) from sentence_progress p
         where p.user_id = u.id and p.episode_id = e.id and p.dictation_best is not null)             as dictation_average,
       (select count(*) from vocab v where v.episode_id = e.id)                                       as words,
       (select count(*) from vocab v join word_progress w on w.lexicon_id = v.lexicon_id
         where v.episode_id = e.id and w.user_id = u.id and w.known)                                  as words_known,
       (select count(*) from vocab v join word_progress w on w.lexicon_id = v.lexicon_id
         where v.episode_id = e.id and w.user_id = u.id and w.due <= current_date)                    as words_due,
       (select count(*) from user_words a where a.user_id = u.id and a.episode_id = e.id)             as words_added
from users u cross join episodes e;
