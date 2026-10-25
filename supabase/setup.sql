-- Mandarin Listening App: database setup
-- Paste this whole file into Supabase → SQL Editor → New query, then click Run.
-- Safe to re-run: it skips anything that already exists.

-- ============================================================
-- Who is allowed to use the app (just you for now; add friends later)
-- ============================================================
create table if not exists public.allowed_users (
  email text primary key
);

insert into public.allowed_users (email)
values ('raz@shipstone.com')
on conflict do nothing;

-- True when the logged-in user's email is on the allowlist.
create or replace function public.is_allowed_user()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.allowed_users
    where email = (auth.jwt() ->> 'email')
  );
$$;

-- ============================================================
-- words: the known-words knowledge base
-- The nightly job only reads this; future importers only write to it.
-- ============================================================
create table if not exists public.words (
  id               bigint generated always as identity primary key,
  hanzi            text not null unique,
  pinyin           text,
  english          text,
  is_function_word boolean not null default false,  -- grammar/pronoun words every sentence needs
  source           text not null default 'seed',     -- seed | discord | gdocs | manual
  added_at         timestamptz not null default now()
);

-- ============================================================
-- sentences: generated nightly, one batch per day
-- ============================================================
create table if not exists public.sentences (
  id          bigint generated always as identity primary key,
  for_date    date not null,                 -- the day this sentence is scheduled for
  position    smallint not null,             -- order within that day (1..12)
  hanzi       text not null,
  pinyin      text not null,
  english     text not null,
  words       text[] not null default '{}',  -- segmented words used, for later weak-word tracking
  voice       text,                          -- TTS voice name
  audio_path  text,                          -- path inside the 'audio' storage bucket
  created_at  timestamptz not null default now(),
  unique (for_date, position)
);

create index if not exists sentences_for_date_idx on public.sentences (for_date);

-- ============================================================
-- reviews: each time you mark a sentence ✓ or ✗
-- ============================================================
create table if not exists public.reviews (
  id           bigint generated always as identity primary key,
  sentence_id  bigint not null references public.sentences (id) on delete cascade,
  user_id      uuid not null default auth.uid() references auth.users (id) on delete cascade,
  correct      boolean not null,
  replays      smallint not null default 0,  -- extra plays before revealing
  speed        real not null default 1.0,    -- playback speed used (1.0, 0.8, 0.65)
  reviewed_at  timestamptz not null default now()
);

create index if not exists reviews_user_time_idx on public.reviews (user_id, reviewed_at desc);

-- ============================================================
-- Row level security: nothing is readable unless you're logged in
-- and on the allowlist. The nightly job uses the secret key, which
-- bypasses these rules.
-- ============================================================
alter table public.allowed_users enable row level security;
alter table public.words         enable row level security;
alter table public.sentences     enable row level security;
alter table public.reviews       enable row level security;

drop policy if exists "allowed users read words" on public.words;
create policy "allowed users read words" on public.words
  for select to authenticated using (public.is_allowed_user());

drop policy if exists "allowed users read sentences" on public.sentences;
create policy "allowed users read sentences" on public.sentences
  for select to authenticated using (public.is_allowed_user());

drop policy if exists "users read own reviews" on public.reviews;
create policy "users read own reviews" on public.reviews
  for select to authenticated
  using (public.is_allowed_user() and user_id = auth.uid());

drop policy if exists "users add own reviews" on public.reviews;
create policy "users add own reviews" on public.reviews
  for insert to authenticated
  with check (public.is_allowed_user() and user_id = auth.uid());

-- ============================================================
-- Storage: private bucket for the sentence audio files
-- ============================================================
insert into storage.buckets (id, name, public)
values ('audio', 'audio', false)
on conflict (id) do nothing;

drop policy if exists "allowed users read audio" on storage.objects;
create policy "allowed users read audio" on storage.objects
  for select to authenticated
  using (bucket_id = 'audio' and public.is_allowed_user());

-- ============================================================
-- Seed: function words (always allowed in sentences)
-- ============================================================
insert into public.words (hanzi, pinyin, english, is_function_word) values
  ('我',   'wǒ',      'I, me',                    true),
  ('你',   'nǐ',      'you',                      true),
  ('他',   'tā',      'he, him',                  true),
  ('她',   'tā',      'she, her',                 true),
  ('我们', 'wǒmen',   'we, us',                   true),
  ('你们', 'nǐmen',   'you (plural)',             true),
  ('他们', 'tāmen',   'they, them',               true),
  ('这',   'zhè',     'this',                     true),
  ('那',   'nà',      'that',                     true),
  ('的',   'de',      'possessive particle',      true),
  ('了',   'le',      'completed-action particle', true),
  ('吗',   'ma',      'yes/no question particle', true),
  ('呢',   'ne',      'question/continuation particle', true),
  ('吧',   'ba',      'suggestion particle',      true),
  ('是',   'shì',     'to be',                    true),
  ('不',   'bù',      'not',                      true),
  ('没',   'méi',     'not (have), did not',      true),
  ('很',   'hěn',     'very',                     true),
  ('也',   'yě',      'also',                     true),
  ('都',   'dōu',     'all, both',                true),
  ('在',   'zài',     'at, in; to be located',    true),
  ('有',   'yǒu',     'to have; there is',        true),
  ('和',   'hé',      'and, with',                true),
  ('个',   'gè',      'general measure word',     true),
  ('一',   'yī',      'one',                      true),
  ('两',   'liǎng',   'two (of something)',       true),
  ('什么', 'shénme',  'what',                     true),
  ('谁',   'shéi',    'who',                      true),
  ('哪儿', 'nǎr',     'where',                    true)
on conflict (hanzi) do nothing;

-- ============================================================
-- Seed: 50 basic content words (placeholder until the real import)
-- ============================================================
insert into public.words (hanzi, pinyin, english) values
  ('人',   'rén',       'person'),
  ('朋友', 'péngyou',   'friend'),
  ('老师', 'lǎoshī',    'teacher'),
  ('学生', 'xuésheng',  'student'),
  ('妈妈', 'māma',      'mom'),
  ('爸爸', 'bàba',      'dad'),
  ('家',   'jiā',       'home, family'),
  ('学校', 'xuéxiào',   'school'),
  ('中国', 'Zhōngguó',  'China'),
  ('中文', 'Zhōngwén',  'Chinese (language)'),
  ('饭',   'fàn',       'meal, rice'),
  ('水',   'shuǐ',      'water'),
  ('茶',   'chá',       'tea'),
  ('咖啡', 'kāfēi',     'coffee'),
  ('书',   'shū',       'book'),
  ('电影', 'diànyǐng',  'movie'),
  ('猫',   'māo',       'cat'),
  ('狗',   'gǒu',       'dog'),
  ('车',   'chē',       'car'),
  ('钱',   'qián',      'money'),
  ('今天', 'jīntiān',   'today'),
  ('明天', 'míngtiān',  'tomorrow'),
  ('昨天', 'zuótiān',   'yesterday'),
  ('现在', 'xiànzài',   'now'),
  ('早上', 'zǎoshang',  'morning'),
  ('晚上', 'wǎnshang',  'evening'),
  ('吃',   'chī',       'to eat'),
  ('喝',   'hē',        'to drink'),
  ('去',   'qù',        'to go'),
  ('来',   'lái',       'to come'),
  ('回',   'huí',       'to return'),
  ('看',   'kàn',       'to look, watch, read'),
  ('听',   'tīng',      'to listen'),
  ('说',   'shuō',      'to speak, say'),
  ('想',   'xiǎng',     'to want; to think'),
  ('喜欢', 'xǐhuan',    'to like'),
  ('买',   'mǎi',       'to buy'),
  ('做',   'zuò',       'to do, make'),
  ('学习', 'xuéxí',     'to study'),
  ('工作', 'gōngzuò',   'to work; job'),
  ('睡觉', 'shuìjiào',  'to sleep'),
  ('好',   'hǎo',       'good'),
  ('大',   'dà',        'big'),
  ('小',   'xiǎo',      'small'),
  ('多',   'duō',       'many, much'),
  ('忙',   'máng',      'busy'),
  ('累',   'lèi',       'tired'),
  ('高兴', 'gāoxìng',   'happy'),
  ('冷',   'lěng',      'cold'),
  ('热',   'rè',        'hot')
on conflict (hanzi) do nothing;
