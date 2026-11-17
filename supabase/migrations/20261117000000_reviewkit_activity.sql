-- ReviewKit users and activity log.
--
-- Run once in the Supabase SQL editor (or `supabase db push`). This file is public, so it contains no
-- e-mail addresses: add the admin afterwards with
--     insert into public.rk_admins (email) values ('<admin e-mail>');
--
-- Who can do what:
--   * The app (anon key, no sign-in) can only call rk_register() and rk_log(). It can't read any table.
--   * An admin signs in to Supabase Auth (e-mail + password). RLS lets them read users and the log and
--     turn users on or off. Nobody can edit or delete log rows through the API.
-- The e-mail the app sends is the one its Claude sign-in reports. Supabase can't check it, so the log
-- records what each PC says, not proof of identity. Admin access is proven by the Supabase Auth sign-in.

create table if not exists public.rk_admins (
  email text primary key check (email = lower(email))
);

create table if not exists public.rk_users (
  email        text primary key check (email = lower(email)),
  claude_org   text,
  auth_method  text,
  machine      text,
  app_version  text,
  is_active    boolean not null default true,
  first_seen   timestamptz not null default now(),
  last_seen    timestamptz not null default now()
);

create table if not exists public.rk_audit_logs (
  id           bigint generated always as identity primary key,
  created_at   timestamptz not null default now(),   -- when Supabase received it
  occurred_at  timestamptz,                           -- when it happened on the PC (can be earlier if offline)
  email        text not null references public.rk_users (email) on delete cascade,
  action       text not null,
  details      jsonb not null default '{}'::jsonb,
  machine      text,
  app_version  text
);
create index if not exists rk_audit_logs_created_idx on public.rk_audit_logs (created_at desc);
create index if not exists rk_audit_logs_email_idx on public.rk_audit_logs (email, created_at desc);

alter table public.rk_admins enable row level security;
alter table public.rk_users enable row level security;
alter table public.rk_audit_logs enable row level security;

revoke all on public.rk_admins, public.rk_users, public.rk_audit_logs from anon, authenticated;
grant select on public.rk_users, public.rk_audit_logs to authenticated;
grant update (is_active) on public.rk_users to authenticated;

-- True when the caller signed in to Supabase Auth with an e-mail listed in rk_admins.
create or replace function public.rk_is_admin()
returns boolean
language sql stable security definer set search_path = public
as $$
  select coalesce((auth.jwt() ->> 'is_anonymous')::boolean, false) = false
     and exists (select 1 from public.rk_admins a where a.email = lower(auth.jwt() ->> 'email'));
$$;

drop policy if exists rk_users_admin_read on public.rk_users;
create policy rk_users_admin_read on public.rk_users for select to authenticated using (public.rk_is_admin());
drop policy if exists rk_users_admin_update on public.rk_users;
create policy rk_users_admin_update on public.rk_users for update to authenticated
  using (public.rk_is_admin()) with check (public.rk_is_admin());
drop policy if exists rk_logs_admin_read on public.rk_audit_logs;
create policy rk_logs_admin_read on public.rk_audit_logs for select to authenticated using (public.rk_is_admin());

-- Called by the app when it starts and when the Claude sign-in changes.
-- Returns {"active": bool, "admin": bool}. Admins are always active.
create or replace function public.rk_register(p_email text, p_org text, p_auth_method text,
                                              p_machine text, p_version text)
returns jsonb
language plpgsql security definer set search_path = public
as $$
declare
  v_email  text := lower(trim(p_email));
  v_active boolean;
  v_admin  boolean;
begin
  if v_email is null or length(v_email) > 254 or v_email !~ '^[^@\s]+@[^@\s]+\.[^@\s]+$' then
    raise exception 'invalid email';
  end if;
  insert into rk_users as u (email, claude_org, auth_method, machine, app_version)
  values (v_email, left(p_org, 200), left(p_auth_method, 40), left(p_machine, 100), left(p_version, 40))
  on conflict (email) do update
    set last_seen   = now(),
        claude_org  = coalesce(excluded.claude_org, u.claude_org),
        auth_method = coalesce(excluded.auth_method, u.auth_method),
        machine     = coalesce(excluded.machine, u.machine),
        app_version = coalesce(excluded.app_version, u.app_version)
  returning is_active into v_active;
  v_admin := exists (select 1 from rk_admins a where a.email = v_email);
  return jsonb_build_object('active', v_active or v_admin, 'admin', v_admin);
end;
$$;

-- Called by the app with a batch of up to 100 events:
-- [{"action": "code_review.run", "details": {...}, "occurred_at": "2026-11-17T09:30:00Z"}, ...]
-- Returns how many rows were written.
create or replace function public.rk_log(p_email text, p_machine text, p_version text, p_events jsonb)
returns integer
language plpgsql security definer set search_path = public
as $$
declare
  v_email text := lower(trim(p_email));
  v_count integer;
begin
  if not exists (select 1 from rk_users where email = v_email) then
    raise exception 'unknown user';
  end if;
  if jsonb_typeof(p_events) <> 'array' or jsonb_array_length(p_events) > 100 then
    raise exception 'events must be an array of at most 100 items';
  end if;
  update rk_users set last_seen = now() where email = v_email;
  insert into rk_audit_logs (email, action, details, occurred_at, machine, app_version)
  select v_email,
         e ->> 'action',
         case when jsonb_typeof(e -> 'details') = 'object' and length((e -> 'details')::text) <= 4000
              then e -> 'details' else '{}'::jsonb end,
         least(coalesce((e ->> 'occurred_at')::timestamptz, now()), now()),
         left(p_machine, 100),
         left(p_version, 40)
  from jsonb_array_elements(p_events) as e
  where (e ->> 'action') ~ '^[a-z_]{1,32}(\.[a-z_]{1,32}){0,2}$';
  get diagnostics v_count = row_count;
  return v_count;
end;
$$;

revoke all on function public.rk_register(text, text, text, text, text) from public;
revoke all on function public.rk_log(text, text, text, jsonb) from public;
revoke all on function public.rk_is_admin() from public;
grant execute on function public.rk_register(text, text, text, text, text) to anon, authenticated;
grant execute on function public.rk_log(text, text, text, jsonb) to anon, authenticated;
grant execute on function public.rk_is_admin() to authenticated;
