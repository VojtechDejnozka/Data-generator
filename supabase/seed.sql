-- LOCAL DEVELOPMENT ONLY (applied by `supabase db reset`). Never run against a hosted project:
-- the passwords below are public.
--
--   demo@story2data.local  / demo-heslo-123   (role user)
--   admin@story2data.local / admin-heslo-123  (role admin)
--
-- The shared demo preset run is generated and uploaded by `scripts/seed_supabase.py`
-- (it needs the backend to build the dataset and Storage to hold it).

do $$
declare
  u record;
begin
  for u in
    select * from (values
      ('00000000-0000-4000-a000-000000000001'::uuid, 'demo@story2data.local', 'demo-heslo-123'),
      ('00000000-0000-4000-a000-000000000002'::uuid, 'admin@story2data.local', 'admin-heslo-123')
    ) as t(id, email, password)
  loop
    insert into auth.users (
      instance_id, id, aud, role, email, encrypted_password, email_confirmed_at,
      raw_app_meta_data, raw_user_meta_data, created_at, updated_at,
      confirmation_token, recovery_token, email_change_token_new, email_change
    ) values (
      '00000000-0000-0000-0000-000000000000', u.id, 'authenticated', 'authenticated', u.email,
      extensions.crypt(u.password, extensions.gen_salt('bf')), now(),
      '{"provider":"email","providers":["email"]}'::jsonb, '{}'::jsonb, now(), now(),
      '', '', '', ''
    ) on conflict (id) do nothing;

    insert into auth.identities (id, user_id, provider_id, identity_data, provider, last_sign_in_at, created_at, updated_at)
    values (gen_random_uuid(), u.id, u.id::text,
            jsonb_build_object('sub', u.id::text, 'email', u.email, 'email_verified', true),
            'email', now(), now(), now())
    on conflict do nothing;
  end loop;
end
$$;

-- handle_new_user() created the profiles; promote and pre-approve the seeded accounts.
update public.profiles set approved = true where id in (
  '00000000-0000-4000-a000-000000000001', '00000000-0000-4000-a000-000000000002');
update public.profiles set role = 'admin' where id = '00000000-0000-4000-a000-000000000002';
