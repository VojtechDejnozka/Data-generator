-- Story2Data: user accounts, run metadata, jobs, usage accounting and storage policies.
--
-- Access model (see docs/AUTH_PLAN.md):
--   * authenticated users READ only their own rows (plus shared demo presets);
--   * every WRITE goes through the backend with the secret (service_role) key after an ownership check,
--     so a user cannot reset their usage, rewrite a verdict or plant a file the backend later loads;
--   * the only user-writable column is profiles.display_name.
-- New tables in `public` are not exposed to the Data API by default (Supabase change enforced 2026-10-30),
-- so all grants are explicit.

create schema if not exists private;
revoke all on schema private from public;
grant usage on schema private to authenticated, service_role;

-- --------------------------------------------------------------------------- helpers
create or replace function private.touch_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- --------------------------------------------------------------------------- profiles
create table public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  email text not null default '',
  display_name text not null default '' check (char_length(display_name) <= 80),
  role text not null default 'user' check (role in ('user', 'admin')),
  status text not null default 'active' check (status in ('active', 'blocked')),
  -- set for invited users and by an admin; required in SIGNUP_MODE=invite_only
  approved boolean not null default false,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
comment on table public.profiles is 'One row per auth user; role/status/approved are admin-managed (never from user_metadata).';

create trigger profiles_touch before update on public.profiles
  for each row execute function private.touch_updated_at();

-- Admin check used by policies. SECURITY DEFINER avoids recursive RLS on profiles; it lives in the
-- non-exposed `private` schema, only reads the caller's own row and pins search_path.
create or replace function private.is_admin()
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1 from public.profiles p
    where p.id = (select auth.uid()) and p.role = 'admin' and p.status = 'active'
  );
$$;
revoke execute on function private.is_admin() from public, anon;
grant execute on function private.is_admin() to authenticated, service_role;

-- Profile row for every new auth user (e-mail sign-up, OAuth, magic link, admin invite).
create or replace function private.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, email, approved)
  values (new.id, coalesce(lower(new.email), ''), new.invited_at is not null)
  on conflict (id) do nothing;
  return new;
end;
$$;
revoke execute on function private.handle_new_user() from public, anon, authenticated;

create trigger on_auth_user_created
  after insert on auth.users
  for each row execute function private.handle_new_user();

-- --------------------------------------------------------------------------- runs
create table public.runs (
  id text primary key check (id ~ '^[A-Za-z0-9_-]{1,64}$'),
  user_id uuid not null references auth.users (id) on delete cascade,
  kind text not null default 'data' check (kind in ('data', 'documents')),
  engine text,
  label text not null default '',
  name text not null default '',
  status text not null default 'running' check (status in ('running', 'done', 'error')),
  parent_run_id text references public.runs (id) on delete set null,
  is_preset boolean not null default false,
  share_token text unique check (share_token is null or char_length(share_token) >= 32),
  summary jsonb not null default '{}'::jsonb,   -- list-view fields: verdict, row_counts, knobs, mock, usage …
  files jsonb not null default '{}'::jsonb,     -- storage index: relpath -> {size, sha256, gz}
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index runs_user_created_idx on public.runs (user_id, created_at desc);
create index runs_parent_idx on public.runs (parent_run_id);
create index runs_preset_idx on public.runs (created_at desc) where is_preset;

create trigger runs_touch before update on public.runs
  for each row execute function private.touch_updated_at();

-- --------------------------------------------------------------------------- jobs (resumable stages)
create table public.jobs (
  id text primary key check (id ~ '^[A-Za-z0-9_-]{1,64}$'),
  user_id uuid not null references auth.users (id) on delete cascade,
  kind text not null,
  run_id text,
  status text not null default 'queued' check (status in ('queued', 'running', 'done', 'error')),
  stage text not null default '',
  phase text not null default '',
  progress real not null default 0,
  state jsonb not null default '{}'::jsonb,
  events jsonb not null default '[]'::jsonb,
  result jsonb,
  error text,
  cancel_requested boolean not null default false,
  lease_until timestamptz,
  attempts integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index jobs_user_created_idx on public.jobs (user_id, created_at desc);

create trigger jobs_touch before update on public.jobs
  for each row execute function private.touch_updated_at();

-- --------------------------------------------------------------------------- planning sessions
create table public.plan_sessions (
  id text primary key check (id ~ '^[A-Za-z0-9_-]{1,64}$'),
  user_id uuid not null references auth.users (id) on delete cascade,
  kind text not null default 'data',
  data jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index plan_sessions_user_idx on public.plan_sessions (user_id, created_at desc);

create trigger plan_sessions_touch before update on public.plan_sessions
  for each row execute function private.touch_updated_at();

-- --------------------------------------------------------------------------- usage (LLM cost accounting)
create table public.usage (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users (id) on delete cascade,
  job_id text,
  run_id text,
  kind text not null default '',
  calls integer not null default 0 check (calls >= 0),
  tokens bigint not null default 0 check (tokens >= 0),
  cost_usd numeric(12, 6) not null default 0,
  mock boolean not null default false,
  created_at timestamptz not null default now()
);
create index usage_user_created_idx on public.usage (user_id, created_at desc);

-- --------------------------------------------------------------------------- RLS
alter table public.profiles enable row level security;
alter table public.runs enable row level security;
alter table public.jobs enable row level security;
alter table public.plan_sessions enable row level security;
alter table public.usage enable row level security;

create policy "profiles: read own or admin" on public.profiles
  for select to authenticated
  using (id = (select auth.uid()) or (select private.is_admin()));

create policy "profiles: update own display name" on public.profiles
  for update to authenticated
  using (id = (select auth.uid()))
  with check (id = (select auth.uid()));

create policy "runs: read own or preset" on public.runs
  for select to authenticated
  using (user_id = (select auth.uid()) or is_preset);

create policy "jobs: read own" on public.jobs
  for select to authenticated
  using (user_id = (select auth.uid()));

create policy "plan_sessions: read own" on public.plan_sessions
  for select to authenticated
  using (user_id = (select auth.uid()));

create policy "usage: read own or admin" on public.usage
  for select to authenticated
  using (user_id = (select auth.uid()) or (select private.is_admin()));

-- --------------------------------------------------------------------------- grants
revoke all on public.profiles, public.runs, public.jobs, public.plan_sessions, public.usage from anon, authenticated;
grant select on public.profiles, public.runs, public.jobs, public.plan_sessions, public.usage to authenticated;
grant update (display_name) on public.profiles to authenticated;
grant all on public.profiles, public.runs, public.jobs, public.plan_sessions, public.usage to service_role;
grant usage, select on sequence public.usage_id_seq to service_role;

-- --------------------------------------------------------------------------- storage
-- Private bucket; objects live under {user_id}/{run_id}/{relpath}. 50 MiB = Free-plan maximum.
insert into storage.buckets (id, name, public, file_size_limit)
values ('runs', 'runs', false, 52428800)
on conflict (id) do update set public = false, file_size_limit = excluded.file_size_limit;

-- Read: own folder, or a preset run (folder owner must match the preset's owner).
-- No INSERT/UPDATE/DELETE policy for authenticated: only the backend (service_role) writes.
create policy "runs bucket: read own or preset" on storage.objects
  for select to authenticated
  using (
    bucket_id = 'runs'
    and (
      (storage.foldername(objects.name))[1] = (select auth.uid())::text
      or exists (
        -- qualify objects.name: an unqualified `name` would resolve to runs.name inside the subquery
        select 1 from public.runs r
        where r.is_preset
          and r.id = (storage.foldername(objects.name))[2]
          and r.user_id::text = (storage.foldername(objects.name))[1]
      )
    )
  );
