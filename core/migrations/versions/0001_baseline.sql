--
-- PostgreSQL database dump
--

-- Dumped from database version 16.14 (Debian 16.14-1.pgdg12+1)
-- Dumped by pg_dump version 16.14 (Debian 16.14-1.pgdg12+1)

--
-- Name: btree_gist; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS btree_gist WITH SCHEMA public;

--
-- Name: pg_trgm; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public;

--
-- Name: vector; Type: EXTENSION; Schema: -; Owner: -
--

CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

--
-- Name: adjudication_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.adjudication_status AS ENUM (
    'running',
    'resolved',
    'escalated',
    'aborted'
);

--
-- Name: adjudication_step_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.adjudication_step_kind AS ENUM (
    'turn',
    'vote',
    'score',
    'escalation',
    'synthesis',
    'intervention',
    'tool_call'
);

--
-- Name: agent_run_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.agent_run_status AS ENUM (
    'queued',
    'running',
    'succeeded',
    'failed',
    'cancelled',
    'blocked'
);

--
-- Name: budget_period; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.budget_period AS ENUM (
    'month',
    'quarter',
    'year',
    'custom'
);

--
-- Name: conservation_adhesion; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.conservation_adhesion AS ENUM (
    'none',
    'requested',
    'active'
);

--
-- Name: conservation_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.conservation_status AS ENUM (
    'out_of_coverage',
    'ade_pending',
    'ade_covered'
);

--
-- Name: constraint_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.constraint_kind AS ENUM (
    'none',
    'SNET',
    'MSO',
    'MFO'
);

--
-- Name: cost_basis; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.cost_basis AS ENUM (
    'local',
    'our_key',
    'byok'
);

--
-- Name: dependency_type; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.dependency_type AS ENUM (
    'FS',
    'SS',
    'FF',
    'SF'
);

--
-- Name: dispatch_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.dispatch_status AS ENUM (
    'pending',
    'approved',
    'dispatched',
    'denied',
    'skipped',
    'failed'
);

--
-- Name: document_type; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.document_type AS ENUM (
    'TD01',
    'TD04'
);

--
-- Name: email_account_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.email_account_status AS ENUM (
    'active',
    'error',
    'disabled'
);

--
-- Name: email_provider; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.email_provider AS ENUM (
    'gmail',
    'imap_generic',
    'proton_bridge'
);

--
-- Name: exec_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.exec_kind AS ENUM (
    'human',
    'llm_agent'
);

--
-- Name: executor_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.executor_kind AS ENUM (
    'human',
    'llm_agent'
);

--
-- Name: google_calendar_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.google_calendar_status AS ENUM (
    'active',
    'error',
    'disabled'
);

--
-- Name: handoff_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.handoff_status AS ENUM (
    'pending',
    'delivered',
    'consumed',
    'cancelled'
);

--
-- Name: identity_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.identity_kind AS ENUM (
    'user',
    'ai_assistant'
);

--
-- Name: invoice_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.invoice_kind AS ENUM (
    'invoice',
    'credit_note'
);

--
-- Name: invoice_state; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.invoice_state AS ENUM (
    'draft',
    'transmitted',
    'delivered',
    'accepted',
    'rejected'
);

--
-- Name: ledger_entry_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.ledger_entry_kind AS ENUM (
    'grant',
    'debit'
);

--
-- Name: necessity; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.necessity AS ENUM (
    'must',
    'should',
    'could'
);

--
-- Name: note_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.note_kind AS ENUM (
    'voice',
    'text',
    'conversation'
);

--
-- Name: note_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.note_status AS ENUM (
    'captured',
    'transcribing',
    'ready',
    'error'
);

--
-- Name: notification_channel; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.notification_channel AS ENUM (
    'telegram',
    'email'
);

--
-- Name: notification_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.notification_status AS ENUM (
    'pending',
    'sent',
    'failed'
);

--
-- Name: payment_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.payment_status AS ENUM (
    'unpaid',
    'paid'
);

--
-- Name: rate_unit; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.rate_unit AS ENUM (
    'token',
    'audio_min',
    'tts_char',
    'gb_month'
);

--
-- Name: recurrence_freq; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.recurrence_freq AS ENUM (
    'daily',
    'weekly',
    'monthly',
    'yearly'
);

--
-- Name: role; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.role AS ENUM (
    'owner',
    'admin',
    'member',
    'guest'
);

--
-- Name: schedule_mode; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.schedule_mode AS ENUM (
    'auto',
    'manual'
);

--
-- Name: sdi_mandate_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.sdi_mandate_status AS ENUM (
    'active',
    'revoked'
);

--
-- Name: sdi_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.sdi_status AS ENUM (
    'none',
    'RC',
    'MC',
    'NS',
    'AT'
);

--
-- Name: storage_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.storage_kind AS ENUM (
    'db',
    's3'
);

--
-- Name: tag_kind; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.tag_kind AS ENUM (
    'generic',
    'client',
    'project',
    'memory_channel'
);

--
-- Name: time_source; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.time_source AS ENUM (
    'timer',
    'manual'
);

--
-- Name: turn_role; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.turn_role AS ENUM (
    'user',
    'assistant'
);

--
-- Name: add_org_member(uuid, uuid, text, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.add_org_member(p_org uuid, p_actor uuid, p_email text, p_role text) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
  v_actor_role text;
  v_target uuid;
BEGIN
  IF p_role NOT IN ('owner', 'admin', 'member', 'guest') THEN
    RAISE EXCEPTION 'member.role_invalid' USING ERRCODE = 'P0001';
  END IF;
  SELECT role::text INTO v_actor_role
  FROM memberships
  WHERE org_id = p_org AND user_id = p_actor;
  IF v_actor_role IS NULL THEN
    RAISE EXCEPTION 'rbac.no_membership' USING ERRCODE = 'P0001';
  END IF;
  IF v_actor_role <> 'owner' THEN
    RAISE EXCEPTION 'rbac.role_insufficient' USING ERRCODE = 'P0001';
  END IF;
  SELECT id INTO v_target FROM users WHERE lower(email) = lower(p_email);
  IF v_target IS NULL THEN
    RAISE EXCEPTION 'member.not_found' USING ERRCODE = 'P0001';
  END IF;
  INSERT INTO memberships (org_id, user_id, role)
  VALUES (p_org, v_target, p_role::role)
  ON CONFLICT (org_id, user_id)
  DO UPDATE SET role = excluded.role, version = memberships.version + 1;
  RETURN v_target;
END
$$;

--
-- Name: authenticate_agent_token(bytea); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.authenticate_agent_token(p_hash bytea, OUT out_token_id uuid, OUT out_user_id uuid, OUT out_org_id uuid, OUT out_scope text, OUT out_assistant_id uuid, OUT out_assistant_scope jsonb, OUT out_assistant_active boolean) RETURNS SETOF record
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    DECLARE
      v_id uuid;
      v_user uuid;
      v_org uuid;
      v_scope text;
      v_expires timestamptz;
      v_revoked timestamptz;
      v_assistant_id uuid;
      v_assistant_scope jsonb;
      v_assistant_active boolean;
      v_prev_org text := current_setting('app.current_org', true);
      v_prev_user text := current_setting('app.current_user', true);
    BEGIN
      PERFORM set_config('app.current_org', '', true);
      PERFORM set_config('app.current_user', '', true);

      SELECT t.id, t.user_id, t.org_id, t.scope, t.expires_at, t.revoked_at,
             t.assistant_id, a.scope, a.is_active
        INTO v_id, v_user, v_org, v_scope, v_expires, v_revoked,
             v_assistant_id, v_assistant_scope, v_assistant_active
        FROM agent_tokens t
        LEFT JOIN ai_assistants a ON a.id = t.assistant_id
        WHERE t.token_hash = p_hash;

      IF v_id IS NULL
         OR v_revoked IS NOT NULL
         OR (v_expires IS NOT NULL AND v_expires <= now())
         OR (v_assistant_id IS NOT NULL AND v_assistant_active IS DISTINCT FROM true) THEN
        PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
        PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
        RETURN;
      END IF;

      PERFORM set_config('app.current_org', v_org::text, true);
      PERFORM set_config('app.current_user', v_user::text, true);
      UPDATE agent_tokens SET last_used_at = now(), updated_at = now()
        WHERE id = v_id;
      PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
      PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);

      out_token_id := v_id;
      out_user_id := v_user;
      out_org_id := v_org;
      out_scope := v_scope;
      out_assistant_id := v_assistant_id;
      out_assistant_scope := v_assistant_scope;
      out_assistant_active := v_assistant_active;
      RETURN NEXT;
    END
    $$;

--
-- Name: consume_telegram_link_code(text, bigint, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.consume_telegram_link_code(p_code text, p_chat_id bigint, p_chat_username text, OUT out_user_id uuid, OUT out_org_id uuid) RETURNS SETOF record
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    DECLARE
      v_user_id uuid;
      v_org_id uuid;
    BEGIN
      UPDATE telegram_link_codes c
      SET consumed_at = now()
      WHERE c.code = p_code
        AND c.consumed_at IS NULL
        AND c.expires_at > now()
      RETURNING c.user_id, c.org_id
      INTO v_user_id, v_org_id;

      IF v_user_id IS NULL THEN
        RETURN;
      END IF;

      INSERT INTO telegram_links (user_id, chat_id, chat_username, linked_at)
      VALUES (v_user_id, p_chat_id, p_chat_username, now())
      ON CONFLICT (user_id) DO UPDATE
        SET chat_id = EXCLUDED.chat_id,
            chat_username = EXCLUDED.chat_username,
            linked_at = now(),
            version = telegram_links.version + 1,
            updated_at = now();

      out_user_id := v_user_id;
      out_org_id := v_org_id;
      RETURN NEXT;
    END
    $$;

--
-- Name: create_default_calendar(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.create_default_calendar(p_org uuid) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    DECLARE
      v_cal uuid;
    BEGIN
      SELECT id INTO v_cal FROM working_calendars
        WHERE org_id = p_org AND is_default LIMIT 1;
      IF v_cal IS NOT NULL THEN
        RETURN v_cal;
      END IF;
      INSERT INTO working_calendars
        (org_id, name, is_default, weekly_hours)
      VALUES (
        p_org, 'Default', true,
        ('{"mon":[["09:00","13:00"],["14:00","18:00"]]}'::jsonb
         || '{"tue":[["09:00","13:00"],["14:00","18:00"]]}'::jsonb
         || '{"wed":[["09:00","13:00"],["14:00","18:00"]]}'::jsonb
         || '{"thu":[["09:00","13:00"],["14:00","18:00"]]}'::jsonb
         || '{"fri":[["09:00","13:00"],["14:00","18:00"]]}'::jsonb
         || '{"sat":[],"sun":[]}'::jsonb)
      ) RETURNING id INTO v_cal;
      RETURN v_cal;
    END
    $$;

--
-- Name: create_default_workflow(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.create_default_workflow(p_org uuid) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    DECLARE
      v_wf uuid;
      s_todo uuid;
      s_prog uuid;
      s_done uuid;
    BEGIN
      SELECT id INTO v_wf FROM workflow_defs
        WHERE org_id = p_org AND is_default LIMIT 1;
      IF v_wf IS NOT NULL THEN
        RETURN v_wf;
      END IF;
      INSERT INTO workflow_defs (org_id, name, is_default)
        VALUES (p_org, 'Default', true) RETURNING id INTO v_wf;
      INSERT INTO workflow_states (org_id, workflow_id, name, ord,
                                   is_initial, is_terminal)
        VALUES (p_org, v_wf, 'todo', 1, true, false)
        RETURNING id INTO s_todo;
      INSERT INTO workflow_states (org_id, workflow_id, name, ord,
                                   is_initial, is_terminal)
        VALUES (p_org, v_wf, 'in_progress', 2, false, false)
        RETURNING id INTO s_prog;
      INSERT INTO workflow_states (org_id, workflow_id, name, ord,
                                   is_initial, is_terminal)
        VALUES (p_org, v_wf, 'done', 3, false, true)
        RETURNING id INTO s_done;
      INSERT INTO workflow_transitions
        (org_id, workflow_id, from_state_id, to_state_id)
      VALUES
        (p_org, v_wf, s_todo, s_prog),
        (p_org, v_wf, s_prog, s_done),
        (p_org, v_wf, s_prog, s_todo),
        (p_org, v_wf, s_done, s_prog);
      RETURN v_wf;
    END
    $$;

--
-- Name: delete_organization(uuid, uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.delete_organization(p_org uuid, p_user uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
  v_prev_org text := current_setting('app.current_org', true);
  v_prev_user text := current_setting('app.current_user', true);
BEGIN
  -- Set GUCs so the FORCE-RLS policies on memberships / organizations
  -- evaluate true for the scoped reads + the org DELETE. Without this
  -- the SECURITY DEFINER body sees zero rows on managed Postgres.
  PERFORM set_config('app.current_org', p_org::text, true);
  PERFORM set_config('app.current_user', p_user::text, true);
  IF NOT EXISTS (
    SELECT 1 FROM memberships
    WHERE org_id = p_org AND user_id = p_user AND role = 'owner'
  ) THEN
    PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
    PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
    RAISE EXCEPTION 'workspace.not_owner' USING ERRCODE = 'P0001';
  END IF;
  -- Count ALL memberships for the user (across orgs). The
  -- p_memberships_self_read policy (from 0051) allows this regardless
  -- of which org the GUC currently points at, since it keys on
  -- app.current_user.
  IF (SELECT count(*) FROM memberships WHERE user_id = p_user) <= 1 THEN
    PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
    PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
    RAISE EXCEPTION 'workspace.sole' USING ERRCODE = 'P0001';
  END IF;
  -- Allow the cascade to purge the append-only audit/ledger rows of
  -- this (about to be deleted) tenant. Transaction-local: never leaks.
  PERFORM set_config('app.allow_org_purge', 'on', true);
  -- ON DELETE CASCADE removes all org-scoped tenant data + memberships.
  DELETE FROM organizations WHERE id = p_org;
  PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
  PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
END
$$;

--
-- Name: forbid_mutation(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.forbid_mutation() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  IF TG_OP = 'DELETE'
     AND coalesce(current_setting('app.allow_org_purge', true), 'off') = 'on'
  THEN
    RETURN OLD;
  END IF;
  RAISE EXCEPTION 'append-only table: % not allowed', TG_OP;
END
$$;

--
-- Name: list_org_members(uuid, uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.list_org_members(p_org uuid, p_user uuid) RETURNS TABLE(user_id uuid, email text, display_name text, role text, created_at timestamp with time zone)
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
-- The RETURNS TABLE OUT names (user_id, role, created_at, ...) shadow
-- the like-named memberships/users columns inside plpgsql; resolve any
-- ambiguous reference to the column, never the OUT variable.
#variable_conflict use_column
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM memberships
    WHERE org_id = p_org AND user_id = p_user
  ) THEN
    RAISE EXCEPTION 'rbac.no_membership' USING ERRCODE = 'P0001';
  END IF;
  RETURN QUERY
  SELECT
    u.id,
    u.email::text,
    u.display_name::text,
    m.role::text,
    m.created_at
  FROM memberships m
  JOIN users u ON u.id = m.user_id
  WHERE m.org_id = p_org
  ORDER BY m.created_at;
END
$$;

--
-- Name: list_user_organizations(uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.list_user_organizations(p_user_id uuid) RETURNS TABLE(org_id uuid, name text, role text, status text)
    LANGUAGE plpgsql STABLE SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
  v_prev_user text := current_setting('app.current_user', true);
BEGIN
  PERFORM set_config('app.current_user', p_user_id::text, true);
  RETURN QUERY
    SELECT o.id, o.name::text, m.role::text, o.status::text
    FROM memberships m
    JOIN organizations o ON o.id = m.org_id
    WHERE m.user_id = p_user_id;
  PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
END
$$;

--
-- Name: oauth_token_diag(bytea); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.oauth_token_diag(p_hash bytea) RETURNS TABLE(out_exists boolean, out_revoked_at timestamp with time zone, out_expires_at timestamp with time zone, out_assistant_id uuid, out_assistant_active boolean)
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    DECLARE
        v_prev_org text := current_setting('app.current_org', true);
        v_prev_user text := current_setting('app.current_user', true);
    BEGIN
        PERFORM set_config('app.current_org', '', true);
        PERFORM set_config('app.current_user', '', true);
        RETURN QUERY
            SELECT
                TRUE,
                t.revoked_at,
                t.expires_at,
                t.assistant_id,
                a.is_active
            FROM agent_tokens t
            LEFT JOIN ai_assistants a ON a.id = t.assistant_id
            WHERE t.token_hash = p_hash;
        IF NOT FOUND THEN
            -- Token hash matches no row.
            RETURN QUERY SELECT FALSE, NULL::timestamptz, NULL::timestamptz,
                                NULL::uuid, NULL::boolean;
        END IF;
        PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
        PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
    END;
    $$;

--
-- Name: provision_organization(text, uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.provision_organization(p_name text, p_user_id uuid) RETURNS uuid
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
  v_org uuid := gen_random_uuid();
  v_prev_org text := current_setting('app.current_org', true);
  v_prev_user text := current_setting('app.current_user', true);
BEGIN
  PERFORM set_config('app.current_org', v_org::text, true);
  PERFORM set_config('app.current_user', p_user_id::text, true);
  INSERT INTO organizations (id, name) VALUES (v_org, p_name);
  INSERT INTO memberships (org_id, user_id, role)
    VALUES (v_org, p_user_id, 'owner');
  PERFORM create_default_workflow(v_org);
  PERFORM create_default_calendar(v_org);
  -- Restore caller's GUCs so a nested call (e.g. signup inside an
  -- outer tenant_session) does not leave app.current_org/_user
  -- pointing at the new org for the rest of the caller's transaction.
  PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
  PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
  RETURN v_org;
END
$$;

--
-- Name: remove_org_member(uuid, uuid, uuid); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.remove_org_member(p_org uuid, p_actor uuid, p_target uuid) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
  v_actor_role text;
  v_target_role text;
  v_owner_count int;
BEGIN
  SELECT role::text INTO v_actor_role
  FROM memberships
  WHERE org_id = p_org AND user_id = p_actor;
  IF v_actor_role IS NULL THEN
    RAISE EXCEPTION 'rbac.no_membership' USING ERRCODE = 'P0001';
  END IF;
  IF v_actor_role <> 'owner' THEN
    RAISE EXCEPTION 'rbac.role_insufficient' USING ERRCODE = 'P0001';
  END IF;
  SELECT role::text INTO v_target_role
  FROM memberships
  WHERE org_id = p_org AND user_id = p_target;
  IF v_target_role IS NULL THEN
    RAISE EXCEPTION 'member.not_found' USING ERRCODE = 'P0001';
  END IF;
  IF v_target_role = 'owner' THEN
    SELECT count(*) INTO v_owner_count
    FROM memberships
    WHERE org_id = p_org AND role = 'owner';
    IF v_owner_count = 1 THEN
      RAISE EXCEPTION 'member.last_owner' USING ERRCODE = 'P0001';
    END IF;
  END IF;
  DELETE FROM memberships
  WHERE org_id = p_org AND user_id = p_target;
END
$$;

--
-- Name: resolve_telegram_chat(bigint); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.resolve_telegram_chat(p_chat_id bigint, OUT out_user_id uuid, OUT out_default_org_id uuid) RETURNS SETOF record
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    DECLARE
      v_user_id uuid;
      v_org_id uuid;
    BEGIN
      -- telegram_links lost FORCE RLS in 0069, so the function owner
      -- reads the link row directly by chat_id.
      SELECT l.user_id INTO v_user_id
      FROM telegram_links l
      WHERE l.chat_id = p_chat_id;

      IF v_user_id IS NULL THEN
        RETURN;
      END IF;

      -- memberships keeps FORCE RLS. Satisfy p_memberships_self_read
      -- (USING user_id = current_user) so this SECURITY DEFINER body can
      -- read the user's own membership rows without BYPASSRLS. Local to
      -- the current (admin_session) transaction.
      PERFORM set_config('app.current_user', v_user_id::text, true);

      SELECT m.org_id INTO v_org_id
      FROM memberships m
      WHERE m.user_id = v_user_id
      ORDER BY m.created_at ASC
      LIMIT 1;

      out_user_id := v_user_id;
      out_default_org_id := v_org_id;
      RETURN NEXT;
    END
    $$;

--
-- Name: sdi_resolve_invoice_org(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.sdi_resolve_invoice_org(p_identificativo text) RETURNS uuid
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
  SELECT org_id FROM invoices WHERE identificativo_sdi = p_identificativo LIMIT 1
$$;

--
-- Name: sdi_resolve_recipient_org(text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.sdi_resolve_recipient_org(p_codice text) RETURNS TABLE(org_id uuid, issuer_profile_id uuid)
    LANGUAGE sql STABLE SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
  SELECT org_id, id FROM issuer_profiles
  WHERE codice_destinatario_ricezione = p_codice
  LIMIT 1
$$;

--
-- Name: set_member_role(uuid, uuid, uuid, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.set_member_role(p_org uuid, p_actor uuid, p_target uuid, p_role text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
  v_actor_role text;
  v_target_role text;
  v_owner_count int;
BEGIN
  IF p_role NOT IN ('owner', 'admin', 'member', 'guest') THEN
    RAISE EXCEPTION 'member.role_invalid' USING ERRCODE = 'P0001';
  END IF;
  SELECT role::text INTO v_actor_role
  FROM memberships
  WHERE org_id = p_org AND user_id = p_actor;
  IF v_actor_role IS NULL THEN
    RAISE EXCEPTION 'rbac.no_membership' USING ERRCODE = 'P0001';
  END IF;
  IF v_actor_role <> 'owner' THEN
    RAISE EXCEPTION 'rbac.role_insufficient' USING ERRCODE = 'P0001';
  END IF;
  SELECT role::text INTO v_target_role
  FROM memberships
  WHERE org_id = p_org AND user_id = p_target;
  IF v_target_role IS NULL THEN
    RAISE EXCEPTION 'member.not_found' USING ERRCODE = 'P0001';
  END IF;
  IF v_target_role = 'owner' AND p_role <> 'owner' THEN
    SELECT count(*) INTO v_owner_count
    FROM memberships
    WHERE org_id = p_org AND role = 'owner';
    IF v_owner_count = 1 THEN
      RAISE EXCEPTION 'member.last_owner' USING ERRCODE = 'P0001';
    END IF;
  END IF;
  UPDATE memberships
  SET role = p_role::role, version = version + 1
  WHERE org_id = p_org AND user_id = p_target;
END
$$;

--
-- Name: set_organization_status(uuid, uuid, text); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.set_organization_status(p_org uuid, p_user uuid, p_status text) RETURNS void
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
DECLARE
  v_prev_org text := current_setting('app.current_org', true);
  v_prev_user text := current_setting('app.current_user', true);
BEGIN
  IF p_status NOT IN ('active', 'archived') THEN
    RAISE EXCEPTION 'workspace.bad_status' USING ERRCODE = 'P0001';
  END IF;
  PERFORM set_config('app.current_org', p_org::text, true);
  PERFORM set_config('app.current_user', p_user::text, true);
  IF NOT EXISTS (
    SELECT 1 FROM memberships
    WHERE org_id = p_org AND user_id = p_user
      AND role IN ('owner', 'admin')
  ) THEN
    PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
    PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
    RAISE EXCEPTION 'workspace.not_owner' USING ERRCODE = 'P0001';
  END IF;
  UPDATE organizations
  SET status = p_status, version = version + 1
  WHERE id = p_org;
  PERFORM set_config('app.current_org', coalesce(v_prev_org, ''), true);
  PERFORM set_config('app.current_user', coalesce(v_prev_user, ''), true);
END
$$;

--
-- Name: sync_identity_on_ai_assistant_handle_update(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.sync_identity_on_ai_assistant_handle_update() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
BEGIN
  IF NEW.handle IS NULL OR NEW.handle = '' THEN
    RETURN NEW;
  END IF;
  IF OLD.handle IS NOT DISTINCT FROM NEW.handle THEN
    RETURN NEW;
  END IF;
  UPDATE identities
     SET handle = NEW.handle
   WHERE ai_assistant_id = NEW.id
     AND kind = 'ai_assistant'
     AND NOT EXISTS (
       SELECT 1 FROM identities other
        WHERE other.org_id = identities.org_id
          AND other.id <> identities.id
          AND other.handle = NEW.handle
     );
  RETURN NEW;
END
$$;

--
-- Name: sync_identity_on_ai_assistant_insert(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.sync_identity_on_ai_assistant_insert() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    BEGIN
      IF NEW.handle IS NOT NULL AND NEW.handle <> '' THEN
        INSERT INTO identities (org_id, kind, handle, ai_assistant_id)
        VALUES (NEW.org_id, 'ai_assistant', NEW.handle, NEW.id)
        ON CONFLICT (org_id, handle) DO NOTHING;
      END IF;
      RETURN NEW;
    END
    $$;

--
-- Name: sync_identity_on_membership_insert(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.sync_identity_on_membership_insert() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    DECLARE
      v_handle text;
    BEGIN
      SELECT handle INTO v_handle FROM users WHERE id = NEW.user_id;
      IF v_handle IS NOT NULL AND v_handle <> '' THEN
        INSERT INTO identities (org_id, kind, handle, user_id)
        VALUES (NEW.org_id, 'user', v_handle, NEW.user_id)
        ON CONFLICT (org_id, handle) DO NOTHING;
      END IF;
      RETURN NEW;
    END
    $$;

--
-- Name: sync_identity_on_user_handle_update(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.sync_identity_on_user_handle_update() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
BEGIN
  IF NEW.handle IS NULL OR NEW.handle = '' THEN
    RETURN NEW;
  END IF;
  IF OLD.handle IS NOT DISTINCT FROM NEW.handle THEN
    RETURN NEW;
  END IF;
  UPDATE identities
     SET handle = NEW.handle
   WHERE user_id = NEW.id
     AND kind = 'user'
     AND NOT EXISTS (
       SELECT 1 FROM identities other
        WHERE other.org_id = identities.org_id
          AND other.id <> identities.id
          AND other.handle = NEW.handle
     );
  RETURN NEW;
END
$$;

--
-- Name: sync_task_assignee_participant(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.sync_task_assignee_participant() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    DECLARE
      v_old_assignee uuid;
      v_new_is_appt boolean;
    BEGIN
      v_old_assignee := CASE WHEN TG_OP = 'UPDATE' THEN OLD.assignee_id ELSE NULL END;
      v_new_is_appt := (
        NEW.assignee_id IS NOT NULL
        AND NEW.start_at IS NOT NULL
        AND NEW.duration_minutes IS NOT NULL
        AND NEW.is_archived = false
        AND NEW.deleted_at IS NULL
      );
      -- Drop the stale assignee mirror when the assignee changes,
      -- the appointment status is lost, or the row is archived/soft-
      -- deleted. ``ON CONFLICT DO NOTHING`` is not enough -- the row
      -- may need to be removed entirely.
      IF v_old_assignee IS NOT NULL
         AND (NOT v_new_is_appt OR v_old_assignee <> NEW.assignee_id) THEN
        DELETE FROM task_participants
          WHERE task_id = NEW.id AND identity_id = v_old_assignee;
      END IF;
      IF v_new_is_appt THEN
        INSERT INTO task_participants
          (task_id, identity_id, org_id, start_at, duration_minutes)
        VALUES
          (NEW.id, NEW.assignee_id, NEW.org_id,
           NEW.start_at, NEW.duration_minutes)
        ON CONFLICT (task_id, identity_id) DO UPDATE
          SET start_at = EXCLUDED.start_at,
              duration_minutes = EXCLUDED.duration_minutes;
      END IF;
      RETURN NEW;
    END
    $$;

--
-- Name: sync_task_participants_window(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.sync_task_participants_window() RETURNS trigger
    LANGUAGE plpgsql SECURITY DEFINER
    SET search_path TO 'public', 'pg_temp'
    AS $$
    BEGIN
      IF NEW.duration_minutes IS NULL OR NEW.start_at IS NULL THEN
        DELETE FROM task_participants WHERE task_id = NEW.id;
      ELSE
        UPDATE task_participants
           SET start_at = NEW.start_at,
               duration_minutes = NEW.duration_minutes
         WHERE task_id = NEW.id
           AND (start_at IS DISTINCT FROM NEW.start_at
                OR duration_minutes IS DISTINCT FROM NEW.duration_minutes);
      END IF;
      RETURN NEW;
    END
    $$;

--
-- Name: tasks_event_end(timestamp with time zone, integer); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.tasks_event_end(t timestamp with time zone, m integer) RETURNS timestamp with time zone
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$
      SELECT t + make_interval(mins => m)
    $$;

--
-- Name: activity_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.activity_log (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    actor_id uuid,
    entity character varying(80) NOT NULL,
    entity_id uuid,
    action character varying(80) NOT NULL,
    diff jsonb,
    ts timestamp with time zone DEFAULT now() NOT NULL,
    actor_kind text DEFAULT 'human_direct'::text NOT NULL,
    actor_subject_id uuid,
    CONSTRAINT ck_activity_log_actor_kind CHECK ((actor_kind = ANY (ARRAY['human_direct'::text, 'human_api'::text, 'human_telegram'::text, 'agent_run'::text, 'mcp_token'::text, 'system'::text])))
);

ALTER TABLE ONLY public.activity_log FORCE ROW LEVEL SECURITY;

--
-- Name: adjudication_steps; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.adjudication_steps (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    adjudication_id uuid NOT NULL,
    step_no integer NOT NULL,
    kind public.adjudication_step_kind NOT NULL,
    payload_json jsonb NOT NULL,
    agent_id character varying(160),
    embedding public.vector(384),
    created_at timestamp with time zone NOT NULL
);

--
-- Name: adjudications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.adjudications (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    task_id uuid,
    question_text text NOT NULL,
    context_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    strategy_id character varying(120) NOT NULL,
    strategy_config_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    status public.adjudication_status NOT NULL,
    outcome_json jsonb,
    confidence numeric(4,3),
    cost_tokens bigint DEFAULT 0 NOT NULL,
    cost_wall_ms bigint DEFAULT 0 NOT NULL,
    started_at timestamp with time zone NOT NULL,
    ended_at timestamp with time zone,
    created_by uuid NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: agent_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_runs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    task_id uuid NOT NULL,
    executor_id uuid,
    status public.agent_run_status NOT NULL,
    steps integer DEFAULT 0 NOT NULL,
    credits_spent numeric(14,4) DEFAULT 0 NOT NULL,
    started_at timestamp with time zone,
    ended_at timestamp with time zone,
    error character varying(500),
    artifact_note_id uuid,
    cancel_requested boolean DEFAULT false NOT NULL,
    blocked_reason character varying(120),
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.agent_runs FORCE ROW LEVEL SECURITY;

--
-- Name: agent_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.agent_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    prefix character varying(20) NOT NULL,
    token_hash bytea NOT NULL,
    scope character varying(32) DEFAULT 'mcp'::character varying NOT NULL,
    expires_at timestamp with time zone,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    assistant_id uuid,
    CONSTRAINT ck_agent_tokens_name_len CHECK (((length((name)::text) >= 1) AND (length((name)::text) <= 120))),
    CONSTRAINT ck_agent_tokens_scope_len CHECK (((length((scope)::text) >= 1) AND (length((scope)::text) <= 32)))
);

--
-- Name: ai_assistants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.ai_assistants (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    label character varying(255) NOT NULL,
    provider character varying(64),
    model_id character varying(128),
    notes text,
    scope jsonb DEFAULT '[]'::jsonb NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    handle character varying(40) DEFAULT ''::character varying NOT NULL,
    CONSTRAINT ck_ai_assistants_label_len CHECK (((length((label)::text) >= 1) AND (length((label)::text) <= 255)))
);

--
-- Name: attachments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.attachments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    note_id uuid,
    task_id uuid,
    filename character varying(255) NOT NULL,
    mime_type character varying(160) NOT NULL,
    size_bytes integer NOT NULL,
    data bytea,
    uploaded_by uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    storage_key character varying(512),
    CONSTRAINT ck_attachments_one_parent CHECK ((((note_id IS NOT NULL) AND (task_id IS NULL)) OR ((note_id IS NULL) AND (task_id IS NOT NULL))))
);

ALTER TABLE ONLY public.attachments FORCE ROW LEVEL SECURITY;

--
-- Name: billing_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.billing_config (
    org_id uuid NOT NULL,
    byok_fee_factor numeric(18,8) DEFAULT 0.0001 NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.billing_config FORCE ROW LEVEL SECURITY;

--
-- Name: blob_sources; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.blob_sources (
    blob_id uuid NOT NULL,
    org_id uuid NOT NULL,
    source_kind character varying(40) NOT NULL,
    source_id character varying(255) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.blob_sources FORCE ROW LEVEL SECURITY;

--
-- Name: budgets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.budgets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    name character varying(160) NOT NULL,
    category character varying(120),
    period_kind public.budget_period NOT NULL,
    period_start date NOT NULL,
    period_end date NOT NULL,
    amount numeric(14,2) NOT NULL,
    currency character varying(3) DEFAULT 'EUR'::character varying NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_budgets_amount CHECK ((amount >= (0)::numeric)),
    CONSTRAINT ck_budgets_period CHECK ((period_end >= period_start))
);

ALTER TABLE ONLY public.budgets FORCE ROW LEVEL SECURITY;

--
-- Name: calendar_holidays; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.calendar_holidays (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    calendar_id uuid NOT NULL,
    day date NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.calendar_holidays FORCE ROW LEVEL SECURITY;

--
-- Name: client_profile; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.client_profile (
    tag_id uuid NOT NULL,
    org_id uuid NOT NULL,
    ragione_sociale character varying(200) NOT NULL,
    id_paese character varying(2),
    id_codice character varying(30),
    codice_fiscale character varying(30),
    indirizzo character varying(200),
    cap character varying(10),
    comune character varying(120),
    provincia character varying(4),
    nazione character varying(2),
    codice_destinatario character varying(7),
    pec character varying(320),
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    description text,
    default_billable boolean DEFAULT true NOT NULL,
    tariffa numeric(12,2),
    valuta character varying(3) DEFAULT 'EUR'::character varying NOT NULL,
    timezone text,
    payment_iban character varying(34),
    nome character varying(60),
    cognome character varying(60),
    invoice_series character varying(20),
    default_condizioni_pagamento character varying(4),
    default_modalita_pagamento character varying(4),
    default_payment_terms_days integer,
    invoice_language character varying(8)
);

ALTER TABLE ONLY public.client_profile FORCE ROW LEVEL SECURITY;

--
-- Name: comments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.comments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    task_id uuid NOT NULL,
    user_id uuid,
    body text NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.comments FORCE ROW LEVEL SECURITY;

--
-- Name: credit_ledger; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.credit_ledger (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    kind public.ledger_entry_kind NOT NULL,
    amount numeric(18,4) NOT NULL,
    operation_id character varying(128),
    reason text,
    balance_after numeric(18,4) NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_credit_ledger_amount CHECK ((amount >= (0)::numeric))
);

ALTER TABLE ONLY public.credit_ledger FORCE ROW LEVEL SECURITY;

--
-- Name: dispatch_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dispatch_requests (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    task_id uuid NOT NULL,
    executor_id uuid,
    status public.dispatch_status DEFAULT 'pending'::public.dispatch_status NOT NULL,
    projected_credit_cost numeric(14,4) DEFAULT 0 NOT NULL,
    agent_run_id uuid,
    requested_at timestamp with time zone DEFAULT now() NOT NULL,
    decided_at timestamp with time zone,
    decided_by uuid,
    reason character varying(200),
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.dispatch_requests FORCE ROW LEVEL SECURITY;

--
-- Name: email_accounts; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.email_accounts (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    provider public.email_provider NOT NULL,
    email_address character varying(320) NOT NULL,
    display_name character varying(200),
    imap_host character varying(255),
    imap_port integer,
    smtp_host character varying(255),
    smtp_port integer,
    secret_encrypted text NOT NULL,
    status public.email_account_status DEFAULT 'active'::public.email_account_status NOT NULL,
    last_sync_at timestamp with time zone,
    last_error text,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.email_accounts FORCE ROW LEVEL SECURITY;

--
-- Name: email_messages; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.email_messages (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    account_id uuid NOT NULL,
    provider_message_id character varying(255) NOT NULL,
    thread_id character varying(255),
    message_id character varying(998),
    in_reply_to character varying(998),
    from_addr character varying(320) NOT NULL,
    to_addrs text NOT NULL,
    subject text,
    body_text text,
    snippet character varying(500),
    received_at timestamp with time zone NOT NULL,
    is_read boolean DEFAULT false NOT NULL,
    raw_size integer,
    linked_task_id uuid,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.email_messages FORCE ROW LEVEL SECURITY;

--
-- Name: email_verification_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.email_verification_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: executors; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.executors (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    kind public.executor_kind NOT NULL,
    name character varying(120) NOT NULL,
    user_id uuid,
    context_switch_cost_minutes integer DEFAULT 0 NOT NULL,
    provider character varying(60),
    model_id character varying(120),
    max_parallel integer DEFAULT 4 NOT NULL,
    credit_budget numeric(14,4),
    credit_rate_per_hour numeric(14,4) DEFAULT 0 NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    capability_tags text[] DEFAULT '{}'::text[] NOT NULL
);

ALTER TABLE ONLY public.executors FORCE ROW LEVEL SECURITY;

--
-- Name: google_calendar_subscriptions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.google_calendar_subscriptions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    our_calendar_id uuid NOT NULL,
    google_calendar_id character varying(320) NOT NULL,
    refresh_token_encrypted text NOT NULL,
    status public.google_calendar_status DEFAULT 'active'::public.google_calendar_status NOT NULL,
    last_sync_at timestamp with time zone,
    last_error text,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.google_calendar_subscriptions FORCE ROW LEVEL SECURITY;

--
-- Name: identities; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.identities (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    kind public.identity_kind NOT NULL,
    handle character varying(40) NOT NULL,
    user_id uuid,
    ai_assistant_id uuid,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_identities_exactly_one_subject CHECK (((user_id IS NOT NULL) <> (ai_assistant_id IS NOT NULL)))
);

--
-- Name: invoice_counters; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invoice_counters (
    org_id uuid NOT NULL,
    series character varying(20) NOT NULL,
    year integer NOT NULL,
    last_number integer DEFAULT 0 NOT NULL,
    issuer_profile_id uuid NOT NULL
);

ALTER TABLE ONLY public.invoice_counters FORCE ROW LEVEL SECURITY;

--
-- Name: invoice_lines; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invoice_lines (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    invoice_id uuid NOT NULL,
    line_no integer NOT NULL,
    description character varying(1000) NOT NULL,
    quantity numeric(12,4) DEFAULT 1 NOT NULL,
    unit_price numeric(14,4) NOT NULL,
    vat_rate numeric(5,2) DEFAULT 22 NOT NULL,
    natura character varying(4),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.invoice_lines FORCE ROW LEVEL SECURITY;

--
-- Name: invoices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invoices (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    client_tag_id uuid NOT NULL,
    kind public.invoice_kind DEFAULT 'invoice'::public.invoice_kind NOT NULL,
    document_type public.document_type DEFAULT 'TD01'::public.document_type NOT NULL,
    parent_invoice_id uuid,
    series character varying(20) DEFAULT 'A'::character varying NOT NULL,
    year integer NOT NULL,
    number integer,
    state public.invoice_state DEFAULT 'draft'::public.invoice_state NOT NULL,
    currency character varying(3) DEFAULT 'EUR'::character varying NOT NULL,
    causale character varying(200),
    taxable numeric(14,2) DEFAULT 0 NOT NULL,
    vat numeric(14,2) DEFAULT 0 NOT NULL,
    total numeric(14,2) DEFAULT 0 NOT NULL,
    identificativo_sdi character varying(40),
    sdi_status public.sdi_status DEFAULT 'none'::public.sdi_status NOT NULL,
    payment_status public.payment_status DEFAULT 'unpaid'::public.payment_status NOT NULL,
    conservation_status public.conservation_status DEFAULT 'out_of_coverage'::public.conservation_status NOT NULL,
    xml text,
    issued_at timestamp with time zone,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    notes text,
    payment_iban character varying(34),
    payment_due_date date,
    issuer_profile_id uuid,
    bollo numeric(14,2) DEFAULT 0 NOT NULL,
    condizioni_pagamento character varying(4),
    modalita_pagamento character varying(4),
    payment_terms_days integer
);

--
-- Name: issuer_profiles; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.issuer_profiles (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    label character varying(120) DEFAULT 'Principale'::character varying NOT NULL,
    regime_fiscale character varying(4) DEFAULT 'RF01'::character varying NOT NULL,
    paese character varying(2) DEFAULT 'IT'::character varying NOT NULL,
    piva character varying(28),
    codice_fiscale character varying(16),
    denominazione character varying(200) NOT NULL,
    indirizzo character varying(200) DEFAULT ''::character varying NOT NULL,
    cap character varying(10) DEFAULT ''::character varying NOT NULL,
    comune character varying(120) DEFAULT ''::character varying NOT NULL,
    provincia character varying(4),
    nazione character varying(2) DEFAULT 'IT'::character varying NOT NULL,
    rea character varying(40),
    is_default boolean DEFAULT false NOT NULL,
    conservation_adhesion public.conservation_adhesion DEFAULT 'none'::public.conservation_adhesion NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    default_iban character varying(34),
    riferimento_normativo character varying(100),
    nome character varying(60),
    cognome character varying(60),
    pec character varying(320),
    email character varying(320),
    telefono character varying(20),
    fax character varying(20),
    default_condizioni_pagamento character varying(4),
    default_modalita_pagamento character varying(4),
    default_payment_terms_days integer,
    codice_destinatario_ricezione character varying(7)
);

ALTER TABLE ONLY public.issuer_profiles FORCE ROW LEVEL SECURITY;

--
-- Name: memberships; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memberships (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    role public.role NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.memberships FORCE ROW LEVEL SECURITY;

--
-- Name: memory_blob_tags; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_blob_tags (
    blob_id uuid NOT NULL,
    org_id uuid NOT NULL,
    tag_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.memory_blob_tags FORCE ROW LEVEL SECURITY;

--
-- Name: memory_blobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_blobs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    project_id uuid,
    namespace character varying(40) DEFAULT 'email'::character varying NOT NULL,
    tier character varying(8) DEFAULT 'hot'::character varying NOT NULL,
    text text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    embedding public.vector(384),
    model_id character varying(160),
    dim integer DEFAULT 384 NOT NULL,
    summary text,
    access_count integer DEFAULT 0 NOT NULL,
    last_accessed_at timestamp with time zone,
    importance numeric(6,4) DEFAULT 0 NOT NULL,
    access_score numeric(12,6) DEFAULT 0 NOT NULL,
    cluster_id uuid,
    fts tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(text, ''::text))) STORED
)
PARTITION BY HASH (org_id);

ALTER TABLE ONLY public.memory_blobs FORCE ROW LEVEL SECURITY;

--
-- Name: memory_blobs_p0; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_blobs_p0 (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    project_id uuid,
    namespace character varying(40) DEFAULT 'email'::character varying NOT NULL,
    tier character varying(8) DEFAULT 'hot'::character varying NOT NULL,
    text text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    embedding public.vector(384),
    model_id character varying(160),
    dim integer DEFAULT 384 NOT NULL,
    summary text,
    access_count integer DEFAULT 0 NOT NULL,
    last_accessed_at timestamp with time zone,
    importance numeric(6,4) DEFAULT 0 NOT NULL,
    access_score numeric(12,6) DEFAULT 0 NOT NULL,
    cluster_id uuid,
    fts tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(text, ''::text))) STORED
);

--
-- Name: memory_blobs_p1; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_blobs_p1 (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    project_id uuid,
    namespace character varying(40) DEFAULT 'email'::character varying NOT NULL,
    tier character varying(8) DEFAULT 'hot'::character varying NOT NULL,
    text text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    embedding public.vector(384),
    model_id character varying(160),
    dim integer DEFAULT 384 NOT NULL,
    summary text,
    access_count integer DEFAULT 0 NOT NULL,
    last_accessed_at timestamp with time zone,
    importance numeric(6,4) DEFAULT 0 NOT NULL,
    access_score numeric(12,6) DEFAULT 0 NOT NULL,
    cluster_id uuid,
    fts tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(text, ''::text))) STORED
);

--
-- Name: memory_blobs_p2; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_blobs_p2 (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    project_id uuid,
    namespace character varying(40) DEFAULT 'email'::character varying NOT NULL,
    tier character varying(8) DEFAULT 'hot'::character varying NOT NULL,
    text text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    embedding public.vector(384),
    model_id character varying(160),
    dim integer DEFAULT 384 NOT NULL,
    summary text,
    access_count integer DEFAULT 0 NOT NULL,
    last_accessed_at timestamp with time zone,
    importance numeric(6,4) DEFAULT 0 NOT NULL,
    access_score numeric(12,6) DEFAULT 0 NOT NULL,
    cluster_id uuid,
    fts tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(text, ''::text))) STORED
);

--
-- Name: memory_blobs_p3; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_blobs_p3 (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    project_id uuid,
    namespace character varying(40) DEFAULT 'email'::character varying NOT NULL,
    tier character varying(8) DEFAULT 'hot'::character varying NOT NULL,
    text text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    embedding public.vector(384),
    model_id character varying(160),
    dim integer DEFAULT 384 NOT NULL,
    summary text,
    access_count integer DEFAULT 0 NOT NULL,
    last_accessed_at timestamp with time zone,
    importance numeric(6,4) DEFAULT 0 NOT NULL,
    access_score numeric(12,6) DEFAULT 0 NOT NULL,
    cluster_id uuid,
    fts tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(text, ''::text))) STORED
);

--
-- Name: memory_blobs_p4; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_blobs_p4 (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    project_id uuid,
    namespace character varying(40) DEFAULT 'email'::character varying NOT NULL,
    tier character varying(8) DEFAULT 'hot'::character varying NOT NULL,
    text text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    embedding public.vector(384),
    model_id character varying(160),
    dim integer DEFAULT 384 NOT NULL,
    summary text,
    access_count integer DEFAULT 0 NOT NULL,
    last_accessed_at timestamp with time zone,
    importance numeric(6,4) DEFAULT 0 NOT NULL,
    access_score numeric(12,6) DEFAULT 0 NOT NULL,
    cluster_id uuid,
    fts tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(text, ''::text))) STORED
);

--
-- Name: memory_blobs_p5; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_blobs_p5 (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    project_id uuid,
    namespace character varying(40) DEFAULT 'email'::character varying NOT NULL,
    tier character varying(8) DEFAULT 'hot'::character varying NOT NULL,
    text text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    embedding public.vector(384),
    model_id character varying(160),
    dim integer DEFAULT 384 NOT NULL,
    summary text,
    access_count integer DEFAULT 0 NOT NULL,
    last_accessed_at timestamp with time zone,
    importance numeric(6,4) DEFAULT 0 NOT NULL,
    access_score numeric(12,6) DEFAULT 0 NOT NULL,
    cluster_id uuid,
    fts tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(text, ''::text))) STORED
);

--
-- Name: memory_blobs_p6; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_blobs_p6 (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    project_id uuid,
    namespace character varying(40) DEFAULT 'email'::character varying NOT NULL,
    tier character varying(8) DEFAULT 'hot'::character varying NOT NULL,
    text text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    embedding public.vector(384),
    model_id character varying(160),
    dim integer DEFAULT 384 NOT NULL,
    summary text,
    access_count integer DEFAULT 0 NOT NULL,
    last_accessed_at timestamp with time zone,
    importance numeric(6,4) DEFAULT 0 NOT NULL,
    access_score numeric(12,6) DEFAULT 0 NOT NULL,
    cluster_id uuid,
    fts tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(text, ''::text))) STORED
);

--
-- Name: memory_blobs_p7; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory_blobs_p7 (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    project_id uuid,
    namespace character varying(40) DEFAULT 'email'::character varying NOT NULL,
    tier character varying(8) DEFAULT 'hot'::character varying NOT NULL,
    text text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    embedding public.vector(384),
    model_id character varying(160),
    dim integer DEFAULT 384 NOT NULL,
    summary text,
    access_count integer DEFAULT 0 NOT NULL,
    last_accessed_at timestamp with time zone,
    importance numeric(6,4) DEFAULT 0 NOT NULL,
    access_score numeric(12,6) DEFAULT 0 NOT NULL,
    cluster_id uuid,
    fts tsvector GENERATED ALWAYS AS (to_tsvector('simple'::regconfig, COALESCE(text, ''::text))) STORED
);

--
-- Name: note_note_link; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.note_note_link (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    parent_note_id uuid NOT NULL,
    child_note_id uuid NOT NULL,
    kind text NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_note_note_link_no_self CHECK ((parent_note_id <> child_note_id)),
    CONSTRAINT note_note_link_kind_check CHECK ((kind = ANY (ARRAY['atom_of'::text, 'references'::text, 'replies_to'::text, 'supersedes'::text])))
);

--
-- Name: note_tags; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.note_tags (
    org_id uuid NOT NULL,
    note_id uuid NOT NULL,
    tag_id uuid NOT NULL
);

ALTER TABLE ONLY public.note_tags FORCE ROW LEVEL SECURITY;

--
-- Name: note_task_link; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.note_task_link (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    note_id uuid NOT NULL,
    task_id uuid NOT NULL,
    kind text NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT note_task_link_kind_check CHECK ((kind = ANY (ARRAY['subject'::text, 'artifact'::text, 'derived_from'::text, 'promoted_from'::text])))
);

--
-- Name: note_turns; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.note_turns (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    note_id uuid NOT NULL,
    role public.turn_role NOT NULL,
    content text NOT NULL,
    ord integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.note_turns FORCE ROW LEVEL SECURITY;

--
-- Name: notes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    project_id uuid,
    kind public.note_kind NOT NULL,
    status public.note_status DEFAULT 'captured'::public.note_status NOT NULL,
    title character varying(300),
    transcript text,
    summary text,
    audio_ref character varying(512),
    audio_seconds integer,
    last_error text,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    is_archived boolean DEFAULT false NOT NULL,
    deleted_at timestamp with time zone,
    maturity text DEFAULT 'seed'::text NOT NULL,
    promoted_at timestamp with time zone,
    CONSTRAINT notes_maturity_check CHECK ((maturity = ANY (ARRAY['seed'::text, 'growing'::text, 'mature'::text, 'dormant'::text])))
);

ALTER TABLE ONLY public.notes FORCE ROW LEVEL SECURITY;

--
-- Name: notification_prefs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_prefs (
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    channel public.notification_channel NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    target character varying(320) DEFAULT ''::character varying NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.notification_prefs FORCE ROW LEVEL SECURITY;

--
-- Name: notifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notifications (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    channel public.notification_channel NOT NULL,
    kind character varying(40) NOT NULL,
    title character varying(300) NOT NULL,
    body text NOT NULL,
    dedupe_key character varying(200),
    status public.notification_status DEFAULT 'pending'::public.notification_status NOT NULL,
    sent_at timestamp with time zone,
    last_error text,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.notifications FORCE ROW LEVEL SECURITY;

--
-- Name: oauth_codes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.oauth_codes (
    code character varying(64) NOT NULL,
    client_id character varying(64) NOT NULL,
    redirect_uri text NOT NULL,
    code_challenge character varying(128) NOT NULL,
    code_challenge_method character varying(16) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: organizations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.organizations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    name character varying(200) NOT NULL,
    fiscal_profile jsonb,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    settings jsonb DEFAULT '{}'::jsonb NOT NULL
);

ALTER TABLE ONLY public.organizations FORCE ROW LEVEL SECURITY;

--
-- Name: password_reset_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.password_reset_tokens (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    user_id uuid NOT NULL,
    token_hash character varying(64) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    used_at timestamp with time zone,
    requested_ip character varying(64),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: project_profile; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.project_profile (
    tag_id uuid NOT NULL,
    org_id uuid NOT NULL,
    client_tag_id uuid,
    budget numeric(14,2),
    workflow_id uuid,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    description text
);

ALTER TABLE ONLY public.project_profile FORCE ROW LEVEL SECURITY;

--
-- Name: rate_cards; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rate_cards (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    model_id character varying(160) NOT NULL,
    provider character varying(80) NOT NULL,
    unit public.rate_unit DEFAULT 'token'::public.rate_unit NOT NULL,
    credits_per_input numeric(18,8) DEFAULT 0 NOT NULL,
    credits_per_output numeric(18,8) DEFAULT 0 NOT NULL,
    provider_cost_per_input numeric(18,8),
    provider_cost_per_output numeric(18,8),
    markup numeric(8,4) DEFAULT 1 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    tier character varying(40),
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.rate_cards FORCE ROW LEVEL SECURITY;

--
-- Name: received_invoices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.received_invoices (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    issuer_profile_id uuid NOT NULL,
    identificativo_sdi character varying(64) NOT NULL,
    nome_file character varying(120) NOT NULL,
    formato_trasmissione character varying(8) NOT NULL,
    sender_id_paese character varying(2) NOT NULL,
    sender_id_codice character varying(28) NOT NULL,
    sender_denominazione character varying(200),
    codice_destinatario character varying(7) NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL,
    raw_xml bytea NOT NULL,
    processing_status character varying(20) DEFAULT 'new'::character varying NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: revoked_tokens; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.revoked_tokens (
    jti uuid NOT NULL,
    revoked_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    revoked_by uuid,
    reason character varying(512),
    subject_id uuid,
    typ character varying(32),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: schedule; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schedule (
    task_id uuid NOT NULL,
    org_id uuid NOT NULL,
    es timestamp with time zone,
    ef timestamp with time zone,
    ls timestamp with time zone,
    lf timestamp with time zone,
    slack_minutes integer,
    on_logical_critical_path boolean DEFAULT false NOT NULL,
    scheduled_start timestamp with time zone,
    scheduled_end timestamp with time zone,
    computed_at timestamp with time zone DEFAULT now() NOT NULL,
    input_fingerprint text,
    on_critical_chain boolean DEFAULT false NOT NULL,
    projected_cost numeric(14,4) DEFAULT 0 NOT NULL,
    assigned_executor_id uuid,
    unassignable boolean DEFAULT false NOT NULL,
    unassignable_reason character varying(200)
);

ALTER TABLE ONLY public.schedule FORCE ROW LEVEL SECURITY;

--
-- Name: sdi_mandates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sdi_mandates (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    issuer_profile_id uuid NOT NULL,
    status public.sdi_mandate_status DEFAULT 'active'::public.sdi_mandate_status NOT NULL,
    scope character varying(40) DEFAULT 'transmit'::character varying NOT NULL,
    reference character varying(200),
    granted_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: sdi_transmission_counters; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sdi_transmission_counters (
    intermediary_id character varying(40) NOT NULL,
    last_number bigint DEFAULT 0 NOT NULL
);

--
-- Name: storage_rates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.storage_rates (
    org_id uuid NOT NULL,
    kind public.storage_kind NOT NULL,
    credits_per_gb_month numeric(18,8) DEFAULT 0 NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.storage_rates FORCE ROW LEVEL SECURITY;

--
-- Name: tag_scopes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tag_scopes (
    org_id uuid NOT NULL,
    tag_id uuid NOT NULL,
    target_tag_id uuid NOT NULL
);

ALTER TABLE ONLY public.tag_scopes FORCE ROW LEVEL SECURITY;

--
-- Name: tags; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tags (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    kind public.tag_kind NOT NULL,
    name character varying(120) NOT NULL,
    color character varying(16),
    status character varying(16) DEFAULT 'active'::character varying NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    system_key character varying(64)
);

ALTER TABLE ONLY public.tags FORCE ROW LEVEL SECURITY;

--
-- Name: task_checklist_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_checklist_items (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    task_id uuid NOT NULL,
    text text NOT NULL,
    done boolean DEFAULT false NOT NULL,
    "position" integer DEFAULT 0 NOT NULL,
    done_at timestamp with time zone,
    done_by uuid,
    created_by uuid,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_task_checklist_items_text_nonempty CHECK ((length(btrim(text)) > 0))
);

--
-- Name: task_collaborators; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_collaborators (
    task_id uuid NOT NULL,
    user_id uuid NOT NULL,
    org_id uuid NOT NULL
);

ALTER TABLE ONLY public.task_collaborators FORCE ROW LEVEL SECURITY;

--
-- Name: task_dependencies; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_dependencies (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    predecessor_id uuid NOT NULL,
    successor_id uuid NOT NULL,
    type public.dependency_type NOT NULL,
    lag_working_minutes integer DEFAULT 0 NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_task_dependencies_no_self CHECK ((predecessor_id <> successor_id))
);

ALTER TABLE ONLY public.task_dependencies FORCE ROW LEVEL SECURITY;

--
-- Name: task_handoffs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_handoffs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    predecessor_task_id uuid NOT NULL,
    successor_task_id uuid NOT NULL,
    from_executor_id uuid,
    to_executor_id uuid,
    message character varying(1000) DEFAULT ''::character varying NOT NULL,
    artifact_note_id uuid,
    status public.handoff_status DEFAULT 'pending'::public.handoff_status NOT NULL,
    delivered_at timestamp with time zone,
    consumed_at timestamp with time zone,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.task_handoffs FORCE ROW LEVEL SECURITY;

--
-- Name: task_participants; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_participants (
    task_id uuid NOT NULL,
    identity_id uuid NOT NULL,
    org_id uuid NOT NULL,
    start_at timestamp with time zone NOT NULL,
    duration_minutes integer NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_task_participants_duration_pos CHECK ((duration_minutes > 0))
);

ALTER TABLE ONLY public.task_participants FORCE ROW LEVEL SECURITY;

--
-- Name: task_recurrences; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_recurrences (
    task_id uuid NOT NULL,
    org_id uuid NOT NULL,
    freq public.recurrence_freq NOT NULL,
    "interval" integer DEFAULT 1 NOT NULL,
    next_run timestamp with time zone NOT NULL,
    until timestamp with time zone,
    active boolean DEFAULT true NOT NULL,
    last_spawned_at timestamp with time zone,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.task_recurrences FORCE ROW LEVEL SECURITY;

--
-- Name: task_relations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_relations (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    task_a_id uuid NOT NULL,
    task_b_id uuid NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_task_relations_ordered CHECK ((task_a_id < task_b_id))
);

--
-- Name: task_reminders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_reminders (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    task_id uuid NOT NULL,
    offset_minutes integer DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_task_reminders_offset CHECK ((offset_minutes >= 0))
);

ALTER TABLE ONLY public.task_reminders FORCE ROW LEVEL SECURITY;

--
-- Name: task_tags; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task_tags (
    task_id uuid NOT NULL,
    tag_id uuid NOT NULL,
    org_id uuid NOT NULL
);

ALTER TABLE ONLY public.task_tags FORCE ROW LEVEL SECURITY;

--
-- Name: tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tasks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    title character varying(300) NOT NULL,
    description text,
    priority smallint DEFAULT 3 NOT NULL,
    start_date date,
    due_date date,
    estimate_effort_h numeric(8,2),
    parent_task_id uuid,
    executor_kind public.exec_kind DEFAULT 'human'::public.exec_kind NOT NULL,
    is_archived boolean DEFAULT false NOT NULL,
    deleted_at timestamp with time zone,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    state_id uuid NOT NULL,
    remaining_effort_h numeric(8,2),
    actual_start timestamp with time zone,
    schedule_mode public.schedule_mode DEFAULT 'auto'::public.schedule_mode NOT NULL,
    constraint_kind public.constraint_kind DEFAULT 'none'::public.constraint_kind NOT NULL,
    constraint_date timestamp with time zone,
    is_milestone boolean DEFAULT false NOT NULL,
    monetary_cost numeric(14,2),
    location character varying(200),
    necessity public.necessity DEFAULT 'should'::public.necessity NOT NULL,
    budget_id uuid,
    importance smallint DEFAULT 4 NOT NULL,
    urgency smallint DEFAULT 4 NOT NULL,
    billable boolean,
    required_capabilities text[] DEFAULT '{}'::text[] NOT NULL,
    offered boolean DEFAULT false NOT NULL,
    owner_id uuid NOT NULL,
    assignee_id uuid,
    created_by_identity_id uuid,
    created_by_token_id uuid,
    start_at timestamp with time zone,
    duration_minutes integer,
    recurrence jsonb,
    external_provider character varying(20),
    external_id character varying(255),
    external_subscription_id uuid,
    CONSTRAINT ck_tasks_duration_positive CHECK (((duration_minutes IS NULL) OR (duration_minutes > 0))),
    CONSTRAINT ck_tasks_event_pairing CHECK (((start_at IS NULL) = (duration_minutes IS NULL)))
);

ALTER TABLE ONLY public.tasks FORCE ROW LEVEL SECURITY;

--
-- Name: telegram_assistant_jobs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.telegram_assistant_jobs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    chat_id bigint NOT NULL,
    update_id bigint NOT NULL,
    prompt_text text NOT NULL,
    status character varying(16) DEFAULT 'pending'::character varying NOT NULL,
    reply_text text,
    error text,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_telegram_assistant_jobs_status CHECK (((status)::text = ANY ((ARRAY['pending'::character varying, 'running'::character varying, 'done'::character varying, 'failed'::character varying])::text[])))
);

--
-- Name: telegram_conversations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.telegram_conversations (
    chat_id bigint NOT NULL,
    turns jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: telegram_link_codes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.telegram_link_codes (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    code character varying(32) NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    consumed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_telegram_link_codes_length CHECK (((length((code)::text) >= 6) AND (length((code)::text) <= 32)))
);

--
-- Name: telegram_links; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.telegram_links (
    user_id uuid NOT NULL,
    chat_id bigint NOT NULL,
    chat_username character varying(64),
    linked_at timestamp with time zone DEFAULT now() NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

--
-- Name: telegram_updates; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.telegram_updates (
    update_id bigint NOT NULL,
    received_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.telegram_updates FORCE ROW LEVEL SECURITY;

--
-- Name: time_entries; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.time_entries (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    task_id uuid NOT NULL,
    user_id uuid NOT NULL,
    started_at timestamp with time zone NOT NULL,
    ended_at timestamp with time zone,
    duration_seconds integer,
    source public.time_source NOT NULL,
    billable boolean DEFAULT true NOT NULL,
    rate_snapshot numeric(12,2),
    currency character varying(3) DEFAULT 'EUR'::character varying NOT NULL,
    memo text,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    executor_kind public.exec_kind DEFAULT 'human'::public.exec_kind NOT NULL,
    parallel boolean DEFAULT false NOT NULL,
    note_id uuid,
    CONSTRAINT ck_time_entries_duration CHECK (((duration_seconds IS NULL) OR (duration_seconds >= 0))),
    CONSTRAINT ck_time_entries_interval CHECK (((ended_at IS NULL) OR (ended_at > started_at)))
);

ALTER TABLE ONLY public.time_entries FORCE ROW LEVEL SECURITY;

--
-- Name: usage_record; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.usage_record (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    operation_id character varying(128) NOT NULL,
    model_id character varying(160),
    op character varying(80) NOT NULL,
    basis public.cost_basis NOT NULL,
    units_in numeric(18,4) DEFAULT 0 NOT NULL,
    units_out numeric(18,4) DEFAULT 0 NOT NULL,
    credits numeric(18,4) NOT NULL,
    created_by uuid,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.usage_record FORCE ROW LEVEL SECURITY;

--
-- Name: user_calendar; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_calendar (
    org_id uuid NOT NULL,
    user_id uuid NOT NULL,
    calendar_id uuid NOT NULL,
    daily_capacity_h numeric(5,2) DEFAULT 8 NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.user_calendar FORCE ROW LEVEL SECURITY;

--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    email character varying(320) NOT NULL,
    password_hash character varying(255) NOT NULL,
    is_active boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    display_name character varying(200),
    is_admin boolean DEFAULT false NOT NULL,
    email_verified_at timestamp with time zone,
    mfa_secret character varying(64),
    mfa_enabled_at timestamp with time zone,
    backup_codes_hash text[],
    failed_login_count integer DEFAULT 0 NOT NULL,
    locked_until timestamp with time zone,
    handle character varying(40) DEFAULT ''::character varying NOT NULL
);

--
-- Name: wallet; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.wallet (
    org_id uuid NOT NULL,
    balance numeric(18,4) DEFAULT 0 NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ck_wallet_balance CHECK ((balance >= (0)::numeric))
);

ALTER TABLE ONLY public.wallet FORCE ROW LEVEL SECURITY;

--
-- Name: workflow_defs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workflow_defs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    description text
);

ALTER TABLE ONLY public.workflow_defs FORCE ROW LEVEL SECURITY;

--
-- Name: workflow_states; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workflow_states (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    workflow_id uuid NOT NULL,
    name character varying(80) NOT NULL,
    ord integer DEFAULT 0 NOT NULL,
    is_initial boolean DEFAULT false NOT NULL,
    is_terminal boolean DEFAULT false NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    is_hidden boolean DEFAULT false NOT NULL,
    description text
);

ALTER TABLE ONLY public.workflow_states FORCE ROW LEVEL SECURITY;

--
-- Name: workflow_transitions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.workflow_transitions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    workflow_id uuid NOT NULL,
    from_state_id uuid NOT NULL,
    to_state_id uuid NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.workflow_transitions FORCE ROW LEVEL SECURITY;

--
-- Name: working_calendars; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.working_calendars (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    org_id uuid NOT NULL,
    name character varying(120) NOT NULL,
    is_default boolean DEFAULT false NOT NULL,
    timezone character varying(64) DEFAULT 'Europe/Rome'::character varying NOT NULL,
    weekly_hours jsonb NOT NULL,
    version bigint DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);

ALTER TABLE ONLY public.working_calendars FORCE ROW LEVEL SECURITY;

--
-- Name: memory_blobs_p0; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs ATTACH PARTITION public.memory_blobs_p0 FOR VALUES WITH (modulus 8, remainder 0);

--
-- Name: memory_blobs_p1; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs ATTACH PARTITION public.memory_blobs_p1 FOR VALUES WITH (modulus 8, remainder 1);

--
-- Name: memory_blobs_p2; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs ATTACH PARTITION public.memory_blobs_p2 FOR VALUES WITH (modulus 8, remainder 2);

--
-- Name: memory_blobs_p3; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs ATTACH PARTITION public.memory_blobs_p3 FOR VALUES WITH (modulus 8, remainder 3);

--
-- Name: memory_blobs_p4; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs ATTACH PARTITION public.memory_blobs_p4 FOR VALUES WITH (modulus 8, remainder 4);

--
-- Name: memory_blobs_p5; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs ATTACH PARTITION public.memory_blobs_p5 FOR VALUES WITH (modulus 8, remainder 5);

--
-- Name: memory_blobs_p6; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs ATTACH PARTITION public.memory_blobs_p6 FOR VALUES WITH (modulus 8, remainder 6);

--
-- Name: memory_blobs_p7; Type: TABLE ATTACH; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs ATTACH PARTITION public.memory_blobs_p7 FOR VALUES WITH (modulus 8, remainder 7);

--
-- Name: activity_log activity_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.activity_log
    ADD CONSTRAINT activity_log_pkey PRIMARY KEY (id);

--
-- Name: adjudication_steps adjudication_steps_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.adjudication_steps
    ADD CONSTRAINT adjudication_steps_pkey PRIMARY KEY (id);

--
-- Name: adjudications adjudications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.adjudications
    ADD CONSTRAINT adjudications_pkey PRIMARY KEY (id);

--
-- Name: billing_config billing_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_config
    ADD CONSTRAINT billing_config_pkey PRIMARY KEY (org_id);

--
-- Name: budgets budgets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.budgets
    ADD CONSTRAINT budgets_pkey PRIMARY KEY (id);

--
-- Name: calendar_holidays calendar_holidays_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calendar_holidays
    ADD CONSTRAINT calendar_holidays_pkey PRIMARY KEY (id);

--
-- Name: client_profile client_profile_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.client_profile
    ADD CONSTRAINT client_profile_pkey PRIMARY KEY (tag_id);

--
-- Name: comments comments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.comments
    ADD CONSTRAINT comments_pkey PRIMARY KEY (id);

--
-- Name: credit_ledger credit_ledger_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_ledger
    ADD CONSTRAINT credit_ledger_pkey PRIMARY KEY (id);

--
-- Name: email_accounts email_accounts_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_accounts
    ADD CONSTRAINT email_accounts_pkey PRIMARY KEY (id);

--
-- Name: email_messages email_messages_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_messages
    ADD CONSTRAINT email_messages_pkey PRIMARY KEY (id);

--
-- Name: email_verification_tokens email_verification_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_verification_tokens
    ADD CONSTRAINT email_verification_tokens_pkey PRIMARY KEY (id);

--
-- Name: email_verification_tokens email_verification_tokens_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_verification_tokens
    ADD CONSTRAINT email_verification_tokens_token_hash_key UNIQUE (token_hash);

--
-- Name: identities identities_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identities
    ADD CONSTRAINT identities_pkey PRIMARY KEY (id);

--
-- Name: invoice_lines invoice_lines_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_lines
    ADD CONSTRAINT invoice_lines_pkey PRIMARY KEY (id);

--
-- Name: invoices invoices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_pkey PRIMARY KEY (id);

--
-- Name: issuer_profiles issuer_profiles_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.issuer_profiles
    ADD CONSTRAINT issuer_profiles_pkey PRIMARY KEY (id);

--
-- Name: memberships memberships_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_pkey PRIMARY KEY (id);

--
-- Name: memory_blobs pk_memory_blobs; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs
    ADD CONSTRAINT pk_memory_blobs PRIMARY KEY (id, org_id);

--
-- Name: memory_blobs_p0 memory_blobs_p0_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs_p0
    ADD CONSTRAINT memory_blobs_p0_pkey PRIMARY KEY (id, org_id);

--
-- Name: memory_blobs_p1 memory_blobs_p1_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs_p1
    ADD CONSTRAINT memory_blobs_p1_pkey PRIMARY KEY (id, org_id);

--
-- Name: memory_blobs_p2 memory_blobs_p2_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs_p2
    ADD CONSTRAINT memory_blobs_p2_pkey PRIMARY KEY (id, org_id);

--
-- Name: memory_blobs_p3 memory_blobs_p3_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs_p3
    ADD CONSTRAINT memory_blobs_p3_pkey PRIMARY KEY (id, org_id);

--
-- Name: memory_blobs_p4 memory_blobs_p4_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs_p4
    ADD CONSTRAINT memory_blobs_p4_pkey PRIMARY KEY (id, org_id);

--
-- Name: memory_blobs_p5 memory_blobs_p5_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs_p5
    ADD CONSTRAINT memory_blobs_p5_pkey PRIMARY KEY (id, org_id);

--
-- Name: memory_blobs_p6 memory_blobs_p6_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs_p6
    ADD CONSTRAINT memory_blobs_p6_pkey PRIMARY KEY (id, org_id);

--
-- Name: memory_blobs_p7 memory_blobs_p7_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs_p7
    ADD CONSTRAINT memory_blobs_p7_pkey PRIMARY KEY (id, org_id);

--
-- Name: task_participants no_overlap_task_participants; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_participants
    ADD CONSTRAINT no_overlap_task_participants EXCLUDE USING gist (identity_id WITH =, tstzrange(start_at, public.tasks_event_end(start_at, duration_minutes)) WITH &&);

--
-- Name: note_note_link note_note_link_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_note_link
    ADD CONSTRAINT note_note_link_pkey PRIMARY KEY (id);

--
-- Name: note_task_link note_task_link_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_task_link
    ADD CONSTRAINT note_task_link_pkey PRIMARY KEY (id);

--
-- Name: note_turns note_turns_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_turns
    ADD CONSTRAINT note_turns_pkey PRIMARY KEY (id);

--
-- Name: notes notes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notes
    ADD CONSTRAINT notes_pkey PRIMARY KEY (id);

--
-- Name: notifications notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_pkey PRIMARY KEY (id);

--
-- Name: oauth_codes oauth_codes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oauth_codes
    ADD CONSTRAINT oauth_codes_pkey PRIMARY KEY (code);

--
-- Name: organizations organizations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.organizations
    ADD CONSTRAINT organizations_pkey PRIMARY KEY (id);

--
-- Name: password_reset_tokens password_reset_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_pkey PRIMARY KEY (id);

--
-- Name: password_reset_tokens password_reset_tokens_token_hash_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_token_hash_key UNIQUE (token_hash);

--
-- Name: agent_runs pk_agent_runs; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT pk_agent_runs PRIMARY KEY (id);

--
-- Name: agent_tokens pk_agent_tokens; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_tokens
    ADD CONSTRAINT pk_agent_tokens PRIMARY KEY (id);

--
-- Name: ai_assistants pk_ai_assistants; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_assistants
    ADD CONSTRAINT pk_ai_assistants PRIMARY KEY (id);

--
-- Name: attachments pk_attachments; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attachments
    ADD CONSTRAINT pk_attachments PRIMARY KEY (id);

--
-- Name: blob_sources pk_blob_sources; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.blob_sources
    ADD CONSTRAINT pk_blob_sources PRIMARY KEY (blob_id, source_kind, source_id);

--
-- Name: dispatch_requests pk_dispatch_requests; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dispatch_requests
    ADD CONSTRAINT pk_dispatch_requests PRIMARY KEY (id);

--
-- Name: executors pk_executors; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.executors
    ADD CONSTRAINT pk_executors PRIMARY KEY (id);

--
-- Name: google_calendar_subscriptions pk_google_calendar_subscriptions; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.google_calendar_subscriptions
    ADD CONSTRAINT pk_google_calendar_subscriptions PRIMARY KEY (id);

--
-- Name: invoice_counters pk_invoice_counters; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_counters
    ADD CONSTRAINT pk_invoice_counters PRIMARY KEY (issuer_profile_id, series, year);

--
-- Name: memory_blob_tags pk_memory_blob_tags; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blob_tags
    ADD CONSTRAINT pk_memory_blob_tags PRIMARY KEY (blob_id, tag_id);

--
-- Name: note_tags pk_note_tags; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_tags
    ADD CONSTRAINT pk_note_tags PRIMARY KEY (note_id, tag_id);

--
-- Name: notification_prefs pk_notification_prefs; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_prefs
    ADD CONSTRAINT pk_notification_prefs PRIMARY KEY (org_id, user_id, channel);

--
-- Name: sdi_transmission_counters pk_sdi_transmission_counters; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sdi_transmission_counters
    ADD CONSTRAINT pk_sdi_transmission_counters PRIMARY KEY (intermediary_id);

--
-- Name: storage_rates pk_storage_rates; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_rates
    ADD CONSTRAINT pk_storage_rates PRIMARY KEY (org_id, kind);

--
-- Name: tag_scopes pk_tag_scopes; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tag_scopes
    ADD CONSTRAINT pk_tag_scopes PRIMARY KEY (tag_id, target_tag_id);

--
-- Name: task_collaborators pk_task_assignees; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_collaborators
    ADD CONSTRAINT pk_task_assignees PRIMARY KEY (task_id, user_id);

--
-- Name: task_handoffs pk_task_handoffs; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_handoffs
    ADD CONSTRAINT pk_task_handoffs PRIMARY KEY (id);

--
-- Name: task_participants pk_task_participants; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_participants
    ADD CONSTRAINT pk_task_participants PRIMARY KEY (task_id, identity_id);

--
-- Name: task_tags pk_task_tags; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_tags
    ADD CONSTRAINT pk_task_tags PRIMARY KEY (task_id, tag_id);

--
-- Name: telegram_assistant_jobs pk_telegram_assistant_jobs; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_assistant_jobs
    ADD CONSTRAINT pk_telegram_assistant_jobs PRIMARY KEY (id);

--
-- Name: telegram_conversations pk_telegram_conversations; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_conversations
    ADD CONSTRAINT pk_telegram_conversations PRIMARY KEY (chat_id);

--
-- Name: telegram_link_codes pk_telegram_link_codes; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_link_codes
    ADD CONSTRAINT pk_telegram_link_codes PRIMARY KEY (id);

--
-- Name: telegram_links pk_telegram_links; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_links
    ADD CONSTRAINT pk_telegram_links PRIMARY KEY (user_id);

--
-- Name: telegram_updates pk_telegram_updates; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_updates
    ADD CONSTRAINT pk_telegram_updates PRIMARY KEY (update_id);

--
-- Name: user_calendar pk_user_calendar; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_calendar
    ADD CONSTRAINT pk_user_calendar PRIMARY KEY (org_id, user_id);

--
-- Name: project_profile project_profile_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_profile
    ADD CONSTRAINT project_profile_pkey PRIMARY KEY (tag_id);

--
-- Name: rate_cards rate_cards_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rate_cards
    ADD CONSTRAINT rate_cards_pkey PRIMARY KEY (id);

--
-- Name: received_invoices received_invoices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.received_invoices
    ADD CONSTRAINT received_invoices_pkey PRIMARY KEY (id);

--
-- Name: revoked_tokens revoked_tokens_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.revoked_tokens
    ADD CONSTRAINT revoked_tokens_pkey PRIMARY KEY (jti);

--
-- Name: schedule schedule_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schedule
    ADD CONSTRAINT schedule_pkey PRIMARY KEY (task_id);

--
-- Name: sdi_mandates sdi_mandates_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sdi_mandates
    ADD CONSTRAINT sdi_mandates_pkey PRIMARY KEY (id);

--
-- Name: tags tags_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tags
    ADD CONSTRAINT tags_pkey PRIMARY KEY (id);

--
-- Name: task_checklist_items task_checklist_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_checklist_items
    ADD CONSTRAINT task_checklist_items_pkey PRIMARY KEY (id);

--
-- Name: task_dependencies task_dependencies_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_dependencies
    ADD CONSTRAINT task_dependencies_pkey PRIMARY KEY (id);

--
-- Name: task_recurrences task_recurrences_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_recurrences
    ADD CONSTRAINT task_recurrences_pkey PRIMARY KEY (task_id);

--
-- Name: task_relations task_relations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_relations
    ADD CONSTRAINT task_relations_pkey PRIMARY KEY (id);

--
-- Name: task_reminders task_reminders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_reminders
    ADD CONSTRAINT task_reminders_pkey PRIMARY KEY (id);

--
-- Name: tasks tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (id);

--
-- Name: time_entries time_entries_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entries
    ADD CONSTRAINT time_entries_pkey PRIMARY KEY (id);

--
-- Name: adjudication_steps uq_adjudication_steps_adjudication_id_step_no; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.adjudication_steps
    ADD CONSTRAINT uq_adjudication_steps_adjudication_id_step_no UNIQUE (adjudication_id, step_no);

--
-- Name: agent_tokens uq_agent_tokens_token_hash; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_tokens
    ADD CONSTRAINT uq_agent_tokens_token_hash UNIQUE (token_hash);

--
-- Name: calendar_holidays uq_calendar_holidays_calendar_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calendar_holidays
    ADD CONSTRAINT uq_calendar_holidays_calendar_id UNIQUE (calendar_id, day);

--
-- Name: credit_ledger uq_credit_ledger_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_ledger
    ADD CONSTRAINT uq_credit_ledger_org_id UNIQUE (org_id, operation_id);

--
-- Name: email_accounts uq_email_accounts_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_accounts
    ADD CONSTRAINT uq_email_accounts_org_id UNIQUE (org_id, email_address);

--
-- Name: email_messages uq_email_messages_account_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_messages
    ADD CONSTRAINT uq_email_messages_account_id UNIQUE (account_id, provider_message_id);

--
-- Name: identities uq_identities_org_handle; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identities
    ADD CONSTRAINT uq_identities_org_handle UNIQUE (org_id, handle);

--
-- Name: invoice_lines uq_invoice_lines_invoice_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_lines
    ADD CONSTRAINT uq_invoice_lines_invoice_id UNIQUE (invoice_id, line_no);

--
-- Name: invoices uq_invoices_issuer; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT uq_invoices_issuer UNIQUE (issuer_profile_id, series, year, number);

--
-- Name: memberships uq_memberships_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT uq_memberships_org_id UNIQUE (org_id, user_id);

--
-- Name: note_note_link uq_note_note_link; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_note_link
    ADD CONSTRAINT uq_note_note_link UNIQUE (parent_note_id, child_note_id, kind);

--
-- Name: note_task_link uq_note_task_link; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_task_link
    ADD CONSTRAINT uq_note_task_link UNIQUE (note_id, task_id, kind);

--
-- Name: note_turns uq_note_turns_note_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_turns
    ADD CONSTRAINT uq_note_turns_note_id UNIQUE (note_id, ord);

--
-- Name: notifications uq_notifications_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT uq_notifications_org_id UNIQUE (org_id, dedupe_key);

--
-- Name: rate_cards uq_rate_cards_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rate_cards
    ADD CONSTRAINT uq_rate_cards_org_id UNIQUE (org_id, model_id);

--
-- Name: tags uq_tags_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tags
    ADD CONSTRAINT uq_tags_org_id UNIQUE (org_id, kind, name);

--
-- Name: task_dependencies uq_task_dependencies_predecessor_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_dependencies
    ADD CONSTRAINT uq_task_dependencies_predecessor_id UNIQUE (predecessor_id, successor_id, type);

--
-- Name: task_relations uq_task_relations_pair; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_relations
    ADD CONSTRAINT uq_task_relations_pair UNIQUE (task_a_id, task_b_id);

--
-- Name: task_reminders uq_task_reminders_task_offset; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_reminders
    ADD CONSTRAINT uq_task_reminders_task_offset UNIQUE (task_id, offset_minutes);

--
-- Name: telegram_assistant_jobs uq_telegram_assistant_jobs_update_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_assistant_jobs
    ADD CONSTRAINT uq_telegram_assistant_jobs_update_id UNIQUE (update_id);

--
-- Name: telegram_link_codes uq_telegram_link_codes_code; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_link_codes
    ADD CONSTRAINT uq_telegram_link_codes_code UNIQUE (code);

--
-- Name: telegram_links uq_telegram_links_chat_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_links
    ADD CONSTRAINT uq_telegram_links_chat_id UNIQUE (chat_id);

--
-- Name: usage_record uq_usage_record_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_record
    ADD CONSTRAINT uq_usage_record_org_id UNIQUE (org_id, operation_id);

--
-- Name: users uq_users_email; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT uq_users_email UNIQUE (email);

--
-- Name: workflow_defs uq_workflow_defs_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_defs
    ADD CONSTRAINT uq_workflow_defs_org_id UNIQUE (org_id, name);

--
-- Name: workflow_states uq_workflow_states_workflow_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_states
    ADD CONSTRAINT uq_workflow_states_workflow_id UNIQUE (workflow_id, name);

--
-- Name: workflow_transitions uq_workflow_transitions_workflow_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_transitions
    ADD CONSTRAINT uq_workflow_transitions_workflow_id UNIQUE (workflow_id, from_state_id, to_state_id);

--
-- Name: working_calendars uq_working_calendars_org_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.working_calendars
    ADD CONSTRAINT uq_working_calendars_org_id UNIQUE (org_id, name);

--
-- Name: usage_record usage_record_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_record
    ADD CONSTRAINT usage_record_pkey PRIMARY KEY (id);

--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);

--
-- Name: wallet wallet_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet
    ADD CONSTRAINT wallet_pkey PRIMARY KEY (org_id);

--
-- Name: workflow_defs workflow_defs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_defs
    ADD CONSTRAINT workflow_defs_pkey PRIMARY KEY (id);

--
-- Name: workflow_states workflow_states_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_states
    ADD CONSTRAINT workflow_states_pkey PRIMARY KEY (id);

--
-- Name: workflow_transitions workflow_transitions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_transitions
    ADD CONSTRAINT workflow_transitions_pkey PRIMARY KEY (id);

--
-- Name: working_calendars working_calendars_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.working_calendars
    ADD CONSTRAINT working_calendars_pkey PRIMARY KEY (id);

--
-- Name: ix_activity_log_actor_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_activity_log_actor_kind ON public.activity_log USING btree (actor_kind);

--
-- Name: ix_activity_log_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_activity_log_org_id ON public.activity_log USING btree (org_id);

--
-- Name: ix_adjudication_steps_adjudication_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_adjudication_steps_adjudication_id ON public.adjudication_steps USING btree (adjudication_id);

--
-- Name: ix_adjudication_steps_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_adjudication_steps_org_id ON public.adjudication_steps USING btree (org_id);

--
-- Name: ix_adjudications_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_adjudications_org_id ON public.adjudications USING btree (org_id);

--
-- Name: ix_adjudications_org_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_adjudications_org_status ON public.adjudications USING btree (org_id, status);

--
-- Name: ix_adjudications_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_adjudications_task_id ON public.adjudications USING btree (task_id);

--
-- Name: ix_agent_runs_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_runs_org_id ON public.agent_runs USING btree (org_id);

--
-- Name: ix_agent_runs_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_runs_task_id ON public.agent_runs USING btree (task_id);

--
-- Name: ix_agent_tokens_assistant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_tokens_assistant_id ON public.agent_tokens USING btree (assistant_id);

--
-- Name: ix_agent_tokens_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_tokens_org_id ON public.agent_tokens USING btree (org_id);

--
-- Name: ix_agent_tokens_org_user_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_agent_tokens_org_user_active ON public.agent_tokens USING btree (org_id, user_id, revoked_at);

--
-- Name: ix_ai_assistants_org_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_ai_assistants_org_user ON public.ai_assistants USING btree (org_id, user_id);

--
-- Name: ix_attachments_note_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_attachments_note_id ON public.attachments USING btree (note_id);

--
-- Name: ix_attachments_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_attachments_org_id ON public.attachments USING btree (org_id);

--
-- Name: ix_attachments_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_attachments_task_id ON public.attachments USING btree (task_id);

--
-- Name: ix_blob_sources_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_blob_sources_org_id ON public.blob_sources USING btree (org_id);

--
-- Name: ix_budgets_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_budgets_org_id ON public.budgets USING btree (org_id);

--
-- Name: ix_calendar_holidays_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_calendar_holidays_org_id ON public.calendar_holidays USING btree (org_id);

--
-- Name: ix_client_profile_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_client_profile_org_id ON public.client_profile USING btree (org_id);

--
-- Name: ix_comments_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_comments_org_id ON public.comments USING btree (org_id);

--
-- Name: ix_comments_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_comments_task_id ON public.comments USING btree (task_id);

--
-- Name: ix_credit_ledger_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_credit_ledger_org_id ON public.credit_ledger USING btree (org_id);

--
-- Name: ix_dispatch_requests_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dispatch_requests_org_id ON public.dispatch_requests USING btree (org_id);

--
-- Name: ix_dispatch_requests_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_dispatch_requests_task_id ON public.dispatch_requests USING btree (task_id);

--
-- Name: ix_email_accounts_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_email_accounts_org_id ON public.email_accounts USING btree (org_id);

--
-- Name: ix_email_messages_account_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_email_messages_account_id ON public.email_messages USING btree (account_id);

--
-- Name: ix_email_messages_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_email_messages_org_id ON public.email_messages USING btree (org_id);

--
-- Name: ix_email_messages_thread_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_email_messages_thread_id ON public.email_messages USING btree (thread_id);

--
-- Name: ix_email_verification_tokens_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_email_verification_tokens_user_id ON public.email_verification_tokens USING btree (user_id);

--
-- Name: ix_executors_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_executors_org_id ON public.executors USING btree (org_id);

--
-- Name: ix_executors_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_executors_user_id ON public.executors USING btree (user_id);

--
-- Name: ix_google_calendar_subscriptions_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_google_calendar_subscriptions_org_id ON public.google_calendar_subscriptions USING btree (org_id);

--
-- Name: ix_google_calendar_subscriptions_our_calendar_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_google_calendar_subscriptions_our_calendar_id ON public.google_calendar_subscriptions USING btree (our_calendar_id);

--
-- Name: ix_google_calendar_subscriptions_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_google_calendar_subscriptions_user_id ON public.google_calendar_subscriptions USING btree (user_id);

--
-- Name: ix_identities_ai_assistant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_identities_ai_assistant_id ON public.identities USING btree (ai_assistant_id);

--
-- Name: ix_identities_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_identities_org_id ON public.identities USING btree (org_id);

--
-- Name: ix_identities_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_identities_user_id ON public.identities USING btree (user_id);

--
-- Name: ix_invoice_lines_invoice_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_invoice_lines_invoice_id ON public.invoice_lines USING btree (invoice_id);

--
-- Name: ix_invoice_lines_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_invoice_lines_org_id ON public.invoice_lines USING btree (org_id);

--
-- Name: ix_invoices_client_tag_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_invoices_client_tag_id ON public.invoices USING btree (client_tag_id);

--
-- Name: ix_invoices_identificativo_sdi; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_invoices_identificativo_sdi ON public.invoices USING btree (identificativo_sdi);

--
-- Name: ix_invoices_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_invoices_org_id ON public.invoices USING btree (org_id);

--
-- Name: ix_issuer_profiles_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_issuer_profiles_org_id ON public.issuer_profiles USING btree (org_id);

--
-- Name: ix_memberships_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memberships_org_id ON public.memberships USING btree (org_id);

--
-- Name: ix_memberships_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memberships_user_id ON public.memberships USING btree (user_id);

--
-- Name: ix_memory_blob_tags_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memory_blob_tags_org_id ON public.memory_blob_tags USING btree (org_id);

--
-- Name: ix_memory_blob_tags_tag_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memory_blob_tags_tag_id ON public.memory_blob_tags USING btree (tag_id);

--
-- Name: ix_memory_blobs_cluster; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memory_blobs_cluster ON ONLY public.memory_blobs USING btree (cluster_id);

--
-- Name: ix_memory_blobs_embedding; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memory_blobs_embedding ON ONLY public.memory_blobs USING hnsw (embedding public.vector_cosine_ops);

--
-- Name: ix_memory_blobs_fts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memory_blobs_fts ON ONLY public.memory_blobs USING gin (fts);

--
-- Name: ix_memory_blobs_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memory_blobs_org_id ON ONLY public.memory_blobs USING btree (org_id);

--
-- Name: ix_memory_blobs_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memory_blobs_project_id ON ONLY public.memory_blobs USING btree (project_id);

--
-- Name: ix_memory_blobs_trgm; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_memory_blobs_trgm ON ONLY public.memory_blobs USING gin (text public.gin_trgm_ops);

--
-- Name: ix_note_note_link_child; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_note_note_link_child ON public.note_note_link USING btree (child_note_id);

--
-- Name: ix_note_note_link_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_note_note_link_org_id ON public.note_note_link USING btree (org_id);

--
-- Name: ix_note_note_link_parent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_note_note_link_parent ON public.note_note_link USING btree (parent_note_id);

--
-- Name: ix_note_tags_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_note_tags_org_id ON public.note_tags USING btree (org_id);

--
-- Name: ix_note_tags_tag_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_note_tags_tag_id ON public.note_tags USING btree (tag_id);

--
-- Name: ix_note_task_link_note; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_note_task_link_note ON public.note_task_link USING btree (note_id);

--
-- Name: ix_note_task_link_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_note_task_link_org_id ON public.note_task_link USING btree (org_id);

--
-- Name: ix_note_task_link_task; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_note_task_link_task ON public.note_task_link USING btree (task_id);

--
-- Name: ix_note_turns_note_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_note_turns_note_id ON public.note_turns USING btree (note_id);

--
-- Name: ix_note_turns_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_note_turns_org_id ON public.note_turns USING btree (org_id);

--
-- Name: ix_notes_maturity; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notes_maturity ON public.notes USING btree (maturity);

--
-- Name: ix_notes_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notes_org_id ON public.notes USING btree (org_id);

--
-- Name: ix_notes_project_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notes_project_id ON public.notes USING btree (project_id);

--
-- Name: ix_notifications_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notifications_org_id ON public.notifications USING btree (org_id);

--
-- Name: ix_notifications_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_notifications_user_id ON public.notifications USING btree (user_id);

--
-- Name: ix_oauth_codes_expires_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_oauth_codes_expires_at ON public.oauth_codes USING btree (expires_at);

--
-- Name: ix_password_reset_tokens_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_password_reset_tokens_user_id ON public.password_reset_tokens USING btree (user_id);

--
-- Name: ix_project_profile_client_tag_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_project_profile_client_tag_id ON public.project_profile USING btree (client_tag_id);

--
-- Name: ix_project_profile_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_project_profile_org_id ON public.project_profile USING btree (org_id);

--
-- Name: ix_rate_cards_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_rate_cards_org_id ON public.rate_cards USING btree (org_id);

--
-- Name: ix_received_invoices_issuer; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_received_invoices_issuer ON public.received_invoices USING btree (issuer_profile_id);

--
-- Name: ix_received_invoices_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_received_invoices_org_id ON public.received_invoices USING btree (org_id);

--
-- Name: ix_schedule_assigned_executor_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_schedule_assigned_executor_id ON public.schedule USING btree (assigned_executor_id);

--
-- Name: ix_schedule_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_schedule_org_id ON public.schedule USING btree (org_id);

--
-- Name: ix_sdi_mandates_issuer_profile_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_sdi_mandates_issuer_profile_id ON public.sdi_mandates USING btree (issuer_profile_id);

--
-- Name: ix_sdi_mandates_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_sdi_mandates_org_id ON public.sdi_mandates USING btree (org_id);

--
-- Name: ix_tag_scopes_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tag_scopes_org_id ON public.tag_scopes USING btree (org_id);

--
-- Name: ix_tag_scopes_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tag_scopes_target ON public.tag_scopes USING btree (target_tag_id);

--
-- Name: ix_tags_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tags_org_id ON public.tags USING btree (org_id);

--
-- Name: ix_task_checklist_items_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_checklist_items_org_id ON public.task_checklist_items USING btree (org_id);

--
-- Name: ix_task_checklist_items_task_id_position; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_checklist_items_task_id_position ON public.task_checklist_items USING btree (task_id, "position");

--
-- Name: ix_task_collaborators_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_collaborators_org_id ON public.task_collaborators USING btree (org_id);

--
-- Name: ix_task_dependencies_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_dependencies_org_id ON public.task_dependencies USING btree (org_id);

--
-- Name: ix_task_dependencies_successor_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_dependencies_successor_id ON public.task_dependencies USING btree (successor_id);

--
-- Name: ix_task_handoffs_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_handoffs_org_id ON public.task_handoffs USING btree (org_id);

--
-- Name: ix_task_handoffs_predecessor_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_handoffs_predecessor_task_id ON public.task_handoffs USING btree (predecessor_task_id);

--
-- Name: ix_task_handoffs_successor_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_handoffs_successor_task_id ON public.task_handoffs USING btree (successor_task_id);

--
-- Name: ix_task_participants_identity_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_participants_identity_id ON public.task_participants USING btree (identity_id);

--
-- Name: ix_task_participants_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_participants_org_id ON public.task_participants USING btree (org_id);

--
-- Name: ix_task_participants_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_participants_task_id ON public.task_participants USING btree (task_id);

--
-- Name: ix_task_recurrences_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_recurrences_org_id ON public.task_recurrences USING btree (org_id);

--
-- Name: ix_task_relations_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_relations_org_id ON public.task_relations USING btree (org_id);

--
-- Name: ix_task_relations_task_b_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_relations_task_b_id ON public.task_relations USING btree (task_b_id);

--
-- Name: ix_task_reminders_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_reminders_org_id ON public.task_reminders USING btree (org_id);

--
-- Name: ix_task_reminders_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_reminders_task_id ON public.task_reminders USING btree (task_id);

--
-- Name: ix_task_tags_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_tags_org_id ON public.task_tags USING btree (org_id);

--
-- Name: ix_task_tags_tag_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_task_tags_tag_id ON public.task_tags USING btree (tag_id);

--
-- Name: ix_tasks_assignee_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_assignee_id ON public.tasks USING btree (assignee_id);

--
-- Name: ix_tasks_created_by_identity_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_created_by_identity_id ON public.tasks USING btree (created_by_identity_id);

--
-- Name: ix_tasks_created_by_token_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_created_by_token_id ON public.tasks USING btree (created_by_token_id);

--
-- Name: ix_tasks_event_start_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_event_start_at ON public.tasks USING btree (start_at) WHERE (duration_minutes IS NOT NULL);

--
-- Name: ix_tasks_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_org_id ON public.tasks USING btree (org_id);

--
-- Name: ix_tasks_owner_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_owner_id ON public.tasks USING btree (owner_id);

--
-- Name: ix_tasks_parent_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_parent_task_id ON public.tasks USING btree (parent_task_id);

--
-- Name: ix_tasks_state_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_state_id ON public.tasks USING btree (state_id);

--
-- Name: ix_telegram_assistant_jobs_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_telegram_assistant_jobs_pending ON public.telegram_assistant_jobs USING btree (created_at) WHERE ((status)::text = 'pending'::text);

--
-- Name: ix_telegram_link_codes_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_telegram_link_codes_org_id ON public.telegram_link_codes USING btree (org_id);

--
-- Name: ix_telegram_link_codes_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_telegram_link_codes_user_id ON public.telegram_link_codes USING btree (user_id);

--
-- Name: ix_telegram_link_codes_user_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_telegram_link_codes_user_pending ON public.telegram_link_codes USING btree (user_id) WHERE (consumed_at IS NULL);

--
-- Name: ix_time_entries_note_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_time_entries_note_id ON public.time_entries USING btree (note_id);

--
-- Name: ix_time_entries_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_time_entries_org_id ON public.time_entries USING btree (org_id);

--
-- Name: ix_time_entries_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_time_entries_task_id ON public.time_entries USING btree (task_id);

--
-- Name: ix_time_entries_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_time_entries_user_id ON public.time_entries USING btree (user_id);

--
-- Name: ix_usage_record_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_usage_record_org_id ON public.usage_record USING btree (org_id);

--
-- Name: ix_workflow_defs_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workflow_defs_org_id ON public.workflow_defs USING btree (org_id);

--
-- Name: ix_workflow_states_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workflow_states_org_id ON public.workflow_states USING btree (org_id);

--
-- Name: ix_workflow_states_workflow_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workflow_states_workflow_id ON public.workflow_states USING btree (workflow_id);

--
-- Name: ix_workflow_transitions_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workflow_transitions_org_id ON public.workflow_transitions USING btree (org_id);

--
-- Name: ix_workflow_transitions_workflow_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_workflow_transitions_workflow_id ON public.workflow_transitions USING btree (workflow_id);

--
-- Name: ix_working_calendars_org_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_working_calendars_org_id ON public.working_calendars USING btree (org_id);

--
-- Name: memory_blobs_p0_cluster_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p0_cluster_id_idx ON public.memory_blobs_p0 USING btree (cluster_id);

--
-- Name: memory_blobs_p0_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p0_embedding_idx ON public.memory_blobs_p0 USING hnsw (embedding public.vector_cosine_ops);

--
-- Name: memory_blobs_p0_fts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p0_fts_idx ON public.memory_blobs_p0 USING gin (fts);

--
-- Name: memory_blobs_p0_org_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p0_org_id_idx ON public.memory_blobs_p0 USING btree (org_id);

--
-- Name: memory_blobs_p0_project_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p0_project_id_idx ON public.memory_blobs_p0 USING btree (project_id);

--
-- Name: memory_blobs_p0_text_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p0_text_idx ON public.memory_blobs_p0 USING gin (text public.gin_trgm_ops);

--
-- Name: memory_blobs_p1_cluster_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p1_cluster_id_idx ON public.memory_blobs_p1 USING btree (cluster_id);

--
-- Name: memory_blobs_p1_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p1_embedding_idx ON public.memory_blobs_p1 USING hnsw (embedding public.vector_cosine_ops);

--
-- Name: memory_blobs_p1_fts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p1_fts_idx ON public.memory_blobs_p1 USING gin (fts);

--
-- Name: memory_blobs_p1_org_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p1_org_id_idx ON public.memory_blobs_p1 USING btree (org_id);

--
-- Name: memory_blobs_p1_project_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p1_project_id_idx ON public.memory_blobs_p1 USING btree (project_id);

--
-- Name: memory_blobs_p1_text_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p1_text_idx ON public.memory_blobs_p1 USING gin (text public.gin_trgm_ops);

--
-- Name: memory_blobs_p2_cluster_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p2_cluster_id_idx ON public.memory_blobs_p2 USING btree (cluster_id);

--
-- Name: memory_blobs_p2_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p2_embedding_idx ON public.memory_blobs_p2 USING hnsw (embedding public.vector_cosine_ops);

--
-- Name: memory_blobs_p2_fts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p2_fts_idx ON public.memory_blobs_p2 USING gin (fts);

--
-- Name: memory_blobs_p2_org_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p2_org_id_idx ON public.memory_blobs_p2 USING btree (org_id);

--
-- Name: memory_blobs_p2_project_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p2_project_id_idx ON public.memory_blobs_p2 USING btree (project_id);

--
-- Name: memory_blobs_p2_text_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p2_text_idx ON public.memory_blobs_p2 USING gin (text public.gin_trgm_ops);

--
-- Name: memory_blobs_p3_cluster_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p3_cluster_id_idx ON public.memory_blobs_p3 USING btree (cluster_id);

--
-- Name: memory_blobs_p3_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p3_embedding_idx ON public.memory_blobs_p3 USING hnsw (embedding public.vector_cosine_ops);

--
-- Name: memory_blobs_p3_fts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p3_fts_idx ON public.memory_blobs_p3 USING gin (fts);

--
-- Name: memory_blobs_p3_org_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p3_org_id_idx ON public.memory_blobs_p3 USING btree (org_id);

--
-- Name: memory_blobs_p3_project_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p3_project_id_idx ON public.memory_blobs_p3 USING btree (project_id);

--
-- Name: memory_blobs_p3_text_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p3_text_idx ON public.memory_blobs_p3 USING gin (text public.gin_trgm_ops);

--
-- Name: memory_blobs_p4_cluster_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p4_cluster_id_idx ON public.memory_blobs_p4 USING btree (cluster_id);

--
-- Name: memory_blobs_p4_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p4_embedding_idx ON public.memory_blobs_p4 USING hnsw (embedding public.vector_cosine_ops);

--
-- Name: memory_blobs_p4_fts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p4_fts_idx ON public.memory_blobs_p4 USING gin (fts);

--
-- Name: memory_blobs_p4_org_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p4_org_id_idx ON public.memory_blobs_p4 USING btree (org_id);

--
-- Name: memory_blobs_p4_project_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p4_project_id_idx ON public.memory_blobs_p4 USING btree (project_id);

--
-- Name: memory_blobs_p4_text_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p4_text_idx ON public.memory_blobs_p4 USING gin (text public.gin_trgm_ops);

--
-- Name: memory_blobs_p5_cluster_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p5_cluster_id_idx ON public.memory_blobs_p5 USING btree (cluster_id);

--
-- Name: memory_blobs_p5_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p5_embedding_idx ON public.memory_blobs_p5 USING hnsw (embedding public.vector_cosine_ops);

--
-- Name: memory_blobs_p5_fts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p5_fts_idx ON public.memory_blobs_p5 USING gin (fts);

--
-- Name: memory_blobs_p5_org_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p5_org_id_idx ON public.memory_blobs_p5 USING btree (org_id);

--
-- Name: memory_blobs_p5_project_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p5_project_id_idx ON public.memory_blobs_p5 USING btree (project_id);

--
-- Name: memory_blobs_p5_text_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p5_text_idx ON public.memory_blobs_p5 USING gin (text public.gin_trgm_ops);

--
-- Name: memory_blobs_p6_cluster_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p6_cluster_id_idx ON public.memory_blobs_p6 USING btree (cluster_id);

--
-- Name: memory_blobs_p6_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p6_embedding_idx ON public.memory_blobs_p6 USING hnsw (embedding public.vector_cosine_ops);

--
-- Name: memory_blobs_p6_fts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p6_fts_idx ON public.memory_blobs_p6 USING gin (fts);

--
-- Name: memory_blobs_p6_org_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p6_org_id_idx ON public.memory_blobs_p6 USING btree (org_id);

--
-- Name: memory_blobs_p6_project_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p6_project_id_idx ON public.memory_blobs_p6 USING btree (project_id);

--
-- Name: memory_blobs_p6_text_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p6_text_idx ON public.memory_blobs_p6 USING gin (text public.gin_trgm_ops);

--
-- Name: memory_blobs_p7_cluster_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p7_cluster_id_idx ON public.memory_blobs_p7 USING btree (cluster_id);

--
-- Name: memory_blobs_p7_embedding_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p7_embedding_idx ON public.memory_blobs_p7 USING hnsw (embedding public.vector_cosine_ops);

--
-- Name: memory_blobs_p7_fts_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p7_fts_idx ON public.memory_blobs_p7 USING gin (fts);

--
-- Name: memory_blobs_p7_org_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p7_org_id_idx ON public.memory_blobs_p7 USING btree (org_id);

--
-- Name: memory_blobs_p7_project_id_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p7_project_id_idx ON public.memory_blobs_p7 USING btree (project_id);

--
-- Name: memory_blobs_p7_text_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_blobs_p7_text_idx ON public.memory_blobs_p7 USING gin (text public.gin_trgm_ops);

--
-- Name: uq_ai_assistants_org_handle; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_ai_assistants_org_handle ON public.ai_assistants USING btree (org_id, handle) WHERE ((handle)::text <> ''::text);

--
-- Name: uq_issuer_codice_destinatario_ricezione; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_issuer_codice_destinatario_ricezione ON public.issuer_profiles USING btree (codice_destinatario_ricezione) WHERE (codice_destinatario_ricezione IS NOT NULL);

--
-- Name: uq_issuer_profiles_default; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_issuer_profiles_default ON public.issuer_profiles USING btree (org_id) WHERE is_default;

--
-- Name: uq_received_invoices_idsdi; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_received_invoices_idsdi ON public.received_invoices USING btree (identificativo_sdi);

--
-- Name: uq_sdi_mandates_active; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_sdi_mandates_active ON public.sdi_mandates USING btree (issuer_profile_id) WHERE (status = 'active'::public.sdi_mandate_status);

--
-- Name: uq_tags_org_system_key; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_tags_org_system_key ON public.tags USING btree (org_id, system_key) WHERE (system_key IS NOT NULL);

--
-- Name: uq_time_entries_running_serial; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_time_entries_running_serial ON public.time_entries USING btree (org_id, user_id) WHERE ((ended_at IS NULL) AND (parallel = false));

--
-- Name: uq_time_entries_running_task; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_time_entries_running_task ON public.time_entries USING btree (org_id, user_id, task_id) WHERE (ended_at IS NULL);

--
-- Name: uq_users_handle; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_users_handle ON public.users USING btree (handle) WHERE ((handle)::text <> ''::text);

--
-- Name: ux_tasks_external_sync; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ux_tasks_external_sync ON public.tasks USING btree (external_subscription_id, external_id) WHERE ((external_subscription_id IS NOT NULL) AND (external_id IS NOT NULL));

--
-- Name: memory_blobs_p0_cluster_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_cluster ATTACH PARTITION public.memory_blobs_p0_cluster_id_idx;

--
-- Name: memory_blobs_p0_embedding_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_embedding ATTACH PARTITION public.memory_blobs_p0_embedding_idx;

--
-- Name: memory_blobs_p0_fts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_fts ATTACH PARTITION public.memory_blobs_p0_fts_idx;

--
-- Name: memory_blobs_p0_org_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_org_id ATTACH PARTITION public.memory_blobs_p0_org_id_idx;

--
-- Name: memory_blobs_p0_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_memory_blobs ATTACH PARTITION public.memory_blobs_p0_pkey;

--
-- Name: memory_blobs_p0_project_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_project_id ATTACH PARTITION public.memory_blobs_p0_project_id_idx;

--
-- Name: memory_blobs_p0_text_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_trgm ATTACH PARTITION public.memory_blobs_p0_text_idx;

--
-- Name: memory_blobs_p1_cluster_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_cluster ATTACH PARTITION public.memory_blobs_p1_cluster_id_idx;

--
-- Name: memory_blobs_p1_embedding_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_embedding ATTACH PARTITION public.memory_blobs_p1_embedding_idx;

--
-- Name: memory_blobs_p1_fts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_fts ATTACH PARTITION public.memory_blobs_p1_fts_idx;

--
-- Name: memory_blobs_p1_org_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_org_id ATTACH PARTITION public.memory_blobs_p1_org_id_idx;

--
-- Name: memory_blobs_p1_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_memory_blobs ATTACH PARTITION public.memory_blobs_p1_pkey;

--
-- Name: memory_blobs_p1_project_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_project_id ATTACH PARTITION public.memory_blobs_p1_project_id_idx;

--
-- Name: memory_blobs_p1_text_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_trgm ATTACH PARTITION public.memory_blobs_p1_text_idx;

--
-- Name: memory_blobs_p2_cluster_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_cluster ATTACH PARTITION public.memory_blobs_p2_cluster_id_idx;

--
-- Name: memory_blobs_p2_embedding_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_embedding ATTACH PARTITION public.memory_blobs_p2_embedding_idx;

--
-- Name: memory_blobs_p2_fts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_fts ATTACH PARTITION public.memory_blobs_p2_fts_idx;

--
-- Name: memory_blobs_p2_org_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_org_id ATTACH PARTITION public.memory_blobs_p2_org_id_idx;

--
-- Name: memory_blobs_p2_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_memory_blobs ATTACH PARTITION public.memory_blobs_p2_pkey;

--
-- Name: memory_blobs_p2_project_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_project_id ATTACH PARTITION public.memory_blobs_p2_project_id_idx;

--
-- Name: memory_blobs_p2_text_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_trgm ATTACH PARTITION public.memory_blobs_p2_text_idx;

--
-- Name: memory_blobs_p3_cluster_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_cluster ATTACH PARTITION public.memory_blobs_p3_cluster_id_idx;

--
-- Name: memory_blobs_p3_embedding_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_embedding ATTACH PARTITION public.memory_blobs_p3_embedding_idx;

--
-- Name: memory_blobs_p3_fts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_fts ATTACH PARTITION public.memory_blobs_p3_fts_idx;

--
-- Name: memory_blobs_p3_org_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_org_id ATTACH PARTITION public.memory_blobs_p3_org_id_idx;

--
-- Name: memory_blobs_p3_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_memory_blobs ATTACH PARTITION public.memory_blobs_p3_pkey;

--
-- Name: memory_blobs_p3_project_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_project_id ATTACH PARTITION public.memory_blobs_p3_project_id_idx;

--
-- Name: memory_blobs_p3_text_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_trgm ATTACH PARTITION public.memory_blobs_p3_text_idx;

--
-- Name: memory_blobs_p4_cluster_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_cluster ATTACH PARTITION public.memory_blobs_p4_cluster_id_idx;

--
-- Name: memory_blobs_p4_embedding_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_embedding ATTACH PARTITION public.memory_blobs_p4_embedding_idx;

--
-- Name: memory_blobs_p4_fts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_fts ATTACH PARTITION public.memory_blobs_p4_fts_idx;

--
-- Name: memory_blobs_p4_org_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_org_id ATTACH PARTITION public.memory_blobs_p4_org_id_idx;

--
-- Name: memory_blobs_p4_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_memory_blobs ATTACH PARTITION public.memory_blobs_p4_pkey;

--
-- Name: memory_blobs_p4_project_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_project_id ATTACH PARTITION public.memory_blobs_p4_project_id_idx;

--
-- Name: memory_blobs_p4_text_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_trgm ATTACH PARTITION public.memory_blobs_p4_text_idx;

--
-- Name: memory_blobs_p5_cluster_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_cluster ATTACH PARTITION public.memory_blobs_p5_cluster_id_idx;

--
-- Name: memory_blobs_p5_embedding_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_embedding ATTACH PARTITION public.memory_blobs_p5_embedding_idx;

--
-- Name: memory_blobs_p5_fts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_fts ATTACH PARTITION public.memory_blobs_p5_fts_idx;

--
-- Name: memory_blobs_p5_org_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_org_id ATTACH PARTITION public.memory_blobs_p5_org_id_idx;

--
-- Name: memory_blobs_p5_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_memory_blobs ATTACH PARTITION public.memory_blobs_p5_pkey;

--
-- Name: memory_blobs_p5_project_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_project_id ATTACH PARTITION public.memory_blobs_p5_project_id_idx;

--
-- Name: memory_blobs_p5_text_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_trgm ATTACH PARTITION public.memory_blobs_p5_text_idx;

--
-- Name: memory_blobs_p6_cluster_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_cluster ATTACH PARTITION public.memory_blobs_p6_cluster_id_idx;

--
-- Name: memory_blobs_p6_embedding_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_embedding ATTACH PARTITION public.memory_blobs_p6_embedding_idx;

--
-- Name: memory_blobs_p6_fts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_fts ATTACH PARTITION public.memory_blobs_p6_fts_idx;

--
-- Name: memory_blobs_p6_org_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_org_id ATTACH PARTITION public.memory_blobs_p6_org_id_idx;

--
-- Name: memory_blobs_p6_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_memory_blobs ATTACH PARTITION public.memory_blobs_p6_pkey;

--
-- Name: memory_blobs_p6_project_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_project_id ATTACH PARTITION public.memory_blobs_p6_project_id_idx;

--
-- Name: memory_blobs_p6_text_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_trgm ATTACH PARTITION public.memory_blobs_p6_text_idx;

--
-- Name: memory_blobs_p7_cluster_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_cluster ATTACH PARTITION public.memory_blobs_p7_cluster_id_idx;

--
-- Name: memory_blobs_p7_embedding_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_embedding ATTACH PARTITION public.memory_blobs_p7_embedding_idx;

--
-- Name: memory_blobs_p7_fts_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_fts ATTACH PARTITION public.memory_blobs_p7_fts_idx;

--
-- Name: memory_blobs_p7_org_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_org_id ATTACH PARTITION public.memory_blobs_p7_org_id_idx;

--
-- Name: memory_blobs_p7_pkey; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.pk_memory_blobs ATTACH PARTITION public.memory_blobs_p7_pkey;

--
-- Name: memory_blobs_p7_project_id_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_project_id ATTACH PARTITION public.memory_blobs_p7_project_id_idx;

--
-- Name: memory_blobs_p7_text_idx; Type: INDEX ATTACH; Schema: public; Owner: -
--

ALTER INDEX public.ix_memory_blobs_trgm ATTACH PARTITION public.memory_blobs_p7_text_idx;

--
-- Name: activity_log trg_activity_log_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_activity_log_append_only BEFORE DELETE OR UPDATE ON public.activity_log FOR EACH ROW EXECUTE FUNCTION public.forbid_mutation();

--
-- Name: credit_ledger trg_credit_ledger_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_credit_ledger_append_only BEFORE DELETE OR UPDATE ON public.credit_ledger FOR EACH ROW EXECUTE FUNCTION public.forbid_mutation();

--
-- Name: ai_assistants trg_sync_identity_on_ai_assistant_handle_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_sync_identity_on_ai_assistant_handle_update AFTER UPDATE OF handle ON public.ai_assistants FOR EACH ROW EXECUTE FUNCTION public.sync_identity_on_ai_assistant_handle_update();

--
-- Name: ai_assistants trg_sync_identity_on_ai_assistant_insert; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_sync_identity_on_ai_assistant_insert AFTER INSERT ON public.ai_assistants FOR EACH ROW EXECUTE FUNCTION public.sync_identity_on_ai_assistant_insert();

--
-- Name: memberships trg_sync_identity_on_membership_insert; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_sync_identity_on_membership_insert AFTER INSERT ON public.memberships FOR EACH ROW EXECUTE FUNCTION public.sync_identity_on_membership_insert();

--
-- Name: users trg_sync_identity_on_user_handle_update; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_sync_identity_on_user_handle_update AFTER UPDATE OF handle ON public.users FOR EACH ROW EXECUTE FUNCTION public.sync_identity_on_user_handle_update();

--
-- Name: tasks trg_sync_task_assignee_participant_ins; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_sync_task_assignee_participant_ins AFTER INSERT ON public.tasks FOR EACH ROW EXECUTE FUNCTION public.sync_task_assignee_participant();

--
-- Name: tasks trg_sync_task_assignee_participant_upd; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_sync_task_assignee_participant_upd AFTER UPDATE OF assignee_id, start_at, duration_minutes, is_archived, deleted_at ON public.tasks FOR EACH ROW EXECUTE FUNCTION public.sync_task_assignee_participant();

--
-- Name: tasks trg_sync_task_participants_window; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_sync_task_participants_window AFTER UPDATE OF start_at, duration_minutes ON public.tasks FOR EACH ROW EXECUTE FUNCTION public.sync_task_participants_window();

--
-- Name: usage_record trg_usage_record_append_only; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_usage_record_append_only BEFORE DELETE OR UPDATE ON public.usage_record FOR EACH ROW EXECUTE FUNCTION public.forbid_mutation();

--
-- Name: adjudication_steps adjudication_steps_adjudication_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.adjudication_steps
    ADD CONSTRAINT adjudication_steps_adjudication_id_fkey FOREIGN KEY (adjudication_id) REFERENCES public.adjudications(id) ON DELETE CASCADE;

--
-- Name: adjudication_steps adjudication_steps_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.adjudication_steps
    ADD CONSTRAINT adjudication_steps_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: adjudications adjudications_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.adjudications
    ADD CONSTRAINT adjudications_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE RESTRICT;

--
-- Name: adjudications adjudications_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.adjudications
    ADD CONSTRAINT adjudications_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: adjudications adjudications_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.adjudications
    ADD CONSTRAINT adjudications_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE SET NULL;

--
-- Name: agent_runs agent_runs_executor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_executor_id_fkey FOREIGN KEY (executor_id) REFERENCES public.executors(id) ON DELETE SET NULL;

--
-- Name: agent_runs agent_runs_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: agent_runs agent_runs_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_runs
    ADD CONSTRAINT agent_runs_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: agent_tokens agent_tokens_assistant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_tokens
    ADD CONSTRAINT agent_tokens_assistant_id_fkey FOREIGN KEY (assistant_id) REFERENCES public.ai_assistants(id) ON DELETE CASCADE;

--
-- Name: agent_tokens agent_tokens_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_tokens
    ADD CONSTRAINT agent_tokens_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: agent_tokens agent_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.agent_tokens
    ADD CONSTRAINT agent_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: ai_assistants ai_assistants_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_assistants
    ADD CONSTRAINT ai_assistants_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: ai_assistants ai_assistants_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ai_assistants
    ADD CONSTRAINT ai_assistants_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: attachments attachments_note_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attachments
    ADD CONSTRAINT attachments_note_id_fkey FOREIGN KEY (note_id) REFERENCES public.notes(id) ON DELETE CASCADE;

--
-- Name: attachments attachments_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attachments
    ADD CONSTRAINT attachments_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: attachments attachments_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attachments
    ADD CONSTRAINT attachments_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: attachments attachments_uploaded_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.attachments
    ADD CONSTRAINT attachments_uploaded_by_fkey FOREIGN KEY (uploaded_by) REFERENCES public.users(id) ON DELETE RESTRICT;

--
-- Name: calendar_holidays calendar_holidays_calendar_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calendar_holidays
    ADD CONSTRAINT calendar_holidays_calendar_id_fkey FOREIGN KEY (calendar_id) REFERENCES public.working_calendars(id) ON DELETE CASCADE;

--
-- Name: client_profile client_profile_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.client_profile
    ADD CONSTRAINT client_profile_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tags(id) ON DELETE CASCADE;

--
-- Name: comments comments_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.comments
    ADD CONSTRAINT comments_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: comments comments_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.comments
    ADD CONSTRAINT comments_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE SET NULL;

--
-- Name: dispatch_requests dispatch_requests_agent_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dispatch_requests
    ADD CONSTRAINT dispatch_requests_agent_run_id_fkey FOREIGN KEY (agent_run_id) REFERENCES public.agent_runs(id) ON DELETE SET NULL;

--
-- Name: dispatch_requests dispatch_requests_executor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dispatch_requests
    ADD CONSTRAINT dispatch_requests_executor_id_fkey FOREIGN KEY (executor_id) REFERENCES public.executors(id) ON DELETE SET NULL;

--
-- Name: dispatch_requests dispatch_requests_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dispatch_requests
    ADD CONSTRAINT dispatch_requests_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: dispatch_requests dispatch_requests_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dispatch_requests
    ADD CONSTRAINT dispatch_requests_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: email_messages email_messages_account_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_messages
    ADD CONSTRAINT email_messages_account_id_fkey FOREIGN KEY (account_id) REFERENCES public.email_accounts(id) ON DELETE CASCADE;

--
-- Name: email_messages email_messages_linked_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_messages
    ADD CONSTRAINT email_messages_linked_task_id_fkey FOREIGN KEY (linked_task_id) REFERENCES public.tasks(id) ON DELETE SET NULL;

--
-- Name: email_verification_tokens email_verification_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_verification_tokens
    ADD CONSTRAINT email_verification_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: executors executors_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.executors
    ADD CONSTRAINT executors_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: executors executors_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.executors
    ADD CONSTRAINT executors_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: activity_log fk_activity_log_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.activity_log
    ADD CONSTRAINT fk_activity_log_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: billing_config fk_billing_config_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.billing_config
    ADD CONSTRAINT fk_billing_config_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: blob_sources fk_blob_sources_blob_id_memory_blobs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.blob_sources
    ADD CONSTRAINT fk_blob_sources_blob_id_memory_blobs FOREIGN KEY (blob_id, org_id) REFERENCES public.memory_blobs(id, org_id) ON DELETE CASCADE;

--
-- Name: blob_sources fk_blob_sources_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.blob_sources
    ADD CONSTRAINT fk_blob_sources_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: budgets fk_budgets_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.budgets
    ADD CONSTRAINT fk_budgets_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: calendar_holidays fk_calendar_holidays_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calendar_holidays
    ADD CONSTRAINT fk_calendar_holidays_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: client_profile fk_client_profile_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.client_profile
    ADD CONSTRAINT fk_client_profile_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: comments fk_comments_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.comments
    ADD CONSTRAINT fk_comments_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: credit_ledger fk_credit_ledger_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.credit_ledger
    ADD CONSTRAINT fk_credit_ledger_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: email_accounts fk_email_accounts_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_accounts
    ADD CONSTRAINT fk_email_accounts_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: email_messages fk_email_messages_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.email_messages
    ADD CONSTRAINT fk_email_messages_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: invoice_counters fk_invoice_counters_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_counters
    ADD CONSTRAINT fk_invoice_counters_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: invoice_lines fk_invoice_lines_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_lines
    ADD CONSTRAINT fk_invoice_lines_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: invoices fk_invoices_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT fk_invoices_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: memberships fk_memberships_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT fk_memberships_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: memory_blob_tags fk_memory_blob_tags_blob_id_memory_blobs; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blob_tags
    ADD CONSTRAINT fk_memory_blob_tags_blob_id_memory_blobs FOREIGN KEY (blob_id, org_id) REFERENCES public.memory_blobs(id, org_id) ON DELETE CASCADE;

--
-- Name: memory_blob_tags fk_memory_blob_tags_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blob_tags
    ADD CONSTRAINT fk_memory_blob_tags_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: memory_blob_tags fk_memory_blob_tags_tag_id_tags; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blob_tags
    ADD CONSTRAINT fk_memory_blob_tags_tag_id_tags FOREIGN KEY (tag_id) REFERENCES public.tags(id) ON DELETE CASCADE;

--
-- Name: memory_blobs fk_memory_blobs_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE public.memory_blobs
    ADD CONSTRAINT fk_memory_blobs_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: memory_blobs_p0 fk_memory_blobs_p0_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs_p0
    ADD CONSTRAINT fk_memory_blobs_p0_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: memory_blobs_p1 fk_memory_blobs_p1_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs_p1
    ADD CONSTRAINT fk_memory_blobs_p1_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: memory_blobs_p2 fk_memory_blobs_p2_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs_p2
    ADD CONSTRAINT fk_memory_blobs_p2_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: memory_blobs_p3 fk_memory_blobs_p3_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs_p3
    ADD CONSTRAINT fk_memory_blobs_p3_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: memory_blobs_p4 fk_memory_blobs_p4_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs_p4
    ADD CONSTRAINT fk_memory_blobs_p4_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: memory_blobs_p5 fk_memory_blobs_p5_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs_p5
    ADD CONSTRAINT fk_memory_blobs_p5_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: memory_blobs_p6 fk_memory_blobs_p6_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs_p6
    ADD CONSTRAINT fk_memory_blobs_p6_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: memory_blobs_p7 fk_memory_blobs_p7_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory_blobs_p7
    ADD CONSTRAINT fk_memory_blobs_p7_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: note_turns fk_note_turns_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_turns
    ADD CONSTRAINT fk_note_turns_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: notes fk_notes_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notes
    ADD CONSTRAINT fk_notes_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: notification_prefs fk_notification_prefs_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_prefs
    ADD CONSTRAINT fk_notification_prefs_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: notifications fk_notifications_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT fk_notifications_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: project_profile fk_project_profile_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_profile
    ADD CONSTRAINT fk_project_profile_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: rate_cards fk_rate_cards_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rate_cards
    ADD CONSTRAINT fk_rate_cards_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: schedule fk_schedule_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schedule
    ADD CONSTRAINT fk_schedule_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: storage_rates fk_storage_rates_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.storage_rates
    ADD CONSTRAINT fk_storage_rates_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: tags fk_tags_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tags
    ADD CONSTRAINT fk_tags_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: task_collaborators fk_task_assignees_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_collaborators
    ADD CONSTRAINT fk_task_assignees_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: task_dependencies fk_task_dependencies_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_dependencies
    ADD CONSTRAINT fk_task_dependencies_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: task_recurrences fk_task_recurrences_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_recurrences
    ADD CONSTRAINT fk_task_recurrences_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: task_tags fk_task_tags_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_tags
    ADD CONSTRAINT fk_task_tags_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: tasks fk_tasks_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT fk_tasks_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: time_entries fk_time_entries_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entries
    ADD CONSTRAINT fk_time_entries_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: usage_record fk_usage_record_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.usage_record
    ADD CONSTRAINT fk_usage_record_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: user_calendar fk_user_calendar_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_calendar
    ADD CONSTRAINT fk_user_calendar_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: wallet fk_wallet_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet
    ADD CONSTRAINT fk_wallet_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: workflow_defs fk_workflow_defs_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_defs
    ADD CONSTRAINT fk_workflow_defs_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: workflow_states fk_workflow_states_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_states
    ADD CONSTRAINT fk_workflow_states_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: workflow_transitions fk_workflow_transitions_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_transitions
    ADD CONSTRAINT fk_workflow_transitions_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: working_calendars fk_working_calendars_org_id_organizations; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.working_calendars
    ADD CONSTRAINT fk_working_calendars_org_id_organizations FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: google_calendar_subscriptions google_calendar_subscriptions_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.google_calendar_subscriptions
    ADD CONSTRAINT google_calendar_subscriptions_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: google_calendar_subscriptions google_calendar_subscriptions_our_calendar_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.google_calendar_subscriptions
    ADD CONSTRAINT google_calendar_subscriptions_our_calendar_id_fkey FOREIGN KEY (our_calendar_id) REFERENCES public.working_calendars(id) ON DELETE CASCADE;

--
-- Name: google_calendar_subscriptions google_calendar_subscriptions_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.google_calendar_subscriptions
    ADD CONSTRAINT google_calendar_subscriptions_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: identities identities_ai_assistant_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identities
    ADD CONSTRAINT identities_ai_assistant_id_fkey FOREIGN KEY (ai_assistant_id) REFERENCES public.ai_assistants(id) ON DELETE CASCADE;

--
-- Name: identities identities_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identities
    ADD CONSTRAINT identities_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: identities identities_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.identities
    ADD CONSTRAINT identities_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: invoice_lines invoice_lines_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoice_lines
    ADD CONSTRAINT invoice_lines_invoice_id_fkey FOREIGN KEY (invoice_id) REFERENCES public.invoices(id) ON DELETE CASCADE;

--
-- Name: invoices invoices_issuer_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_issuer_profile_id_fkey FOREIGN KEY (issuer_profile_id) REFERENCES public.issuer_profiles(id) ON DELETE RESTRICT;

--
-- Name: invoices invoices_parent_invoice_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invoices
    ADD CONSTRAINT invoices_parent_invoice_id_fkey FOREIGN KEY (parent_invoice_id) REFERENCES public.invoices(id) ON DELETE RESTRICT;

--
-- Name: issuer_profiles issuer_profiles_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.issuer_profiles
    ADD CONSTRAINT issuer_profiles_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: memberships memberships_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memberships
    ADD CONSTRAINT memberships_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: note_note_link note_note_link_child_note_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_note_link
    ADD CONSTRAINT note_note_link_child_note_id_fkey FOREIGN KEY (child_note_id) REFERENCES public.notes(id) ON DELETE CASCADE;

--
-- Name: note_note_link note_note_link_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_note_link
    ADD CONSTRAINT note_note_link_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.identities(id) ON DELETE SET NULL;

--
-- Name: note_note_link note_note_link_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_note_link
    ADD CONSTRAINT note_note_link_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: note_note_link note_note_link_parent_note_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_note_link
    ADD CONSTRAINT note_note_link_parent_note_id_fkey FOREIGN KEY (parent_note_id) REFERENCES public.notes(id) ON DELETE CASCADE;

--
-- Name: note_tags note_tags_note_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_tags
    ADD CONSTRAINT note_tags_note_id_fkey FOREIGN KEY (note_id) REFERENCES public.notes(id) ON DELETE CASCADE;

--
-- Name: note_tags note_tags_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_tags
    ADD CONSTRAINT note_tags_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: note_tags note_tags_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_tags
    ADD CONSTRAINT note_tags_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tags(id) ON DELETE CASCADE;

--
-- Name: note_task_link note_task_link_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_task_link
    ADD CONSTRAINT note_task_link_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.identities(id) ON DELETE SET NULL;

--
-- Name: note_task_link note_task_link_note_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_task_link
    ADD CONSTRAINT note_task_link_note_id_fkey FOREIGN KEY (note_id) REFERENCES public.notes(id) ON DELETE CASCADE;

--
-- Name: note_task_link note_task_link_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_task_link
    ADD CONSTRAINT note_task_link_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: note_task_link note_task_link_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_task_link
    ADD CONSTRAINT note_task_link_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: note_turns note_turns_note_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.note_turns
    ADD CONSTRAINT note_turns_note_id_fkey FOREIGN KEY (note_id) REFERENCES public.notes(id) ON DELETE CASCADE;

--
-- Name: notification_prefs notification_prefs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_prefs
    ADD CONSTRAINT notification_prefs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: notifications notifications_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: password_reset_tokens password_reset_tokens_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.password_reset_tokens
    ADD CONSTRAINT password_reset_tokens_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: project_profile project_profile_client_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_profile
    ADD CONSTRAINT project_profile_client_tag_id_fkey FOREIGN KEY (client_tag_id) REFERENCES public.tags(id) ON DELETE SET NULL;

--
-- Name: project_profile project_profile_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.project_profile
    ADD CONSTRAINT project_profile_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tags(id) ON DELETE CASCADE;

--
-- Name: received_invoices received_invoices_issuer_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.received_invoices
    ADD CONSTRAINT received_invoices_issuer_profile_id_fkey FOREIGN KEY (issuer_profile_id) REFERENCES public.issuer_profiles(id) ON DELETE RESTRICT;

--
-- Name: received_invoices received_invoices_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.received_invoices
    ADD CONSTRAINT received_invoices_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: schedule schedule_assigned_executor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schedule
    ADD CONSTRAINT schedule_assigned_executor_id_fkey FOREIGN KEY (assigned_executor_id) REFERENCES public.executors(id) ON DELETE SET NULL;

--
-- Name: schedule schedule_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schedule
    ADD CONSTRAINT schedule_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: sdi_mandates sdi_mandates_issuer_profile_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sdi_mandates
    ADD CONSTRAINT sdi_mandates_issuer_profile_id_fkey FOREIGN KEY (issuer_profile_id) REFERENCES public.issuer_profiles(id) ON DELETE CASCADE;

--
-- Name: sdi_mandates sdi_mandates_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sdi_mandates
    ADD CONSTRAINT sdi_mandates_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: tag_scopes tag_scopes_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tag_scopes
    ADD CONSTRAINT tag_scopes_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: tag_scopes tag_scopes_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tag_scopes
    ADD CONSTRAINT tag_scopes_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tags(id) ON DELETE CASCADE;

--
-- Name: tag_scopes tag_scopes_target_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tag_scopes
    ADD CONSTRAINT tag_scopes_target_tag_id_fkey FOREIGN KEY (target_tag_id) REFERENCES public.tags(id) ON DELETE CASCADE;

--
-- Name: task_collaborators task_assignees_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_collaborators
    ADD CONSTRAINT task_assignees_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: task_collaborators task_assignees_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_collaborators
    ADD CONSTRAINT task_assignees_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: task_checklist_items task_checklist_items_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_checklist_items
    ADD CONSTRAINT task_checklist_items_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE SET NULL;

--
-- Name: task_checklist_items task_checklist_items_done_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_checklist_items
    ADD CONSTRAINT task_checklist_items_done_by_fkey FOREIGN KEY (done_by) REFERENCES public.users(id) ON DELETE SET NULL;

--
-- Name: task_checklist_items task_checklist_items_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_checklist_items
    ADD CONSTRAINT task_checklist_items_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: task_checklist_items task_checklist_items_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_checklist_items
    ADD CONSTRAINT task_checklist_items_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: task_dependencies task_dependencies_predecessor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_dependencies
    ADD CONSTRAINT task_dependencies_predecessor_id_fkey FOREIGN KEY (predecessor_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: task_dependencies task_dependencies_successor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_dependencies
    ADD CONSTRAINT task_dependencies_successor_id_fkey FOREIGN KEY (successor_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: task_handoffs task_handoffs_from_executor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_handoffs
    ADD CONSTRAINT task_handoffs_from_executor_id_fkey FOREIGN KEY (from_executor_id) REFERENCES public.executors(id) ON DELETE SET NULL;

--
-- Name: task_handoffs task_handoffs_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_handoffs
    ADD CONSTRAINT task_handoffs_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: task_handoffs task_handoffs_predecessor_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_handoffs
    ADD CONSTRAINT task_handoffs_predecessor_task_id_fkey FOREIGN KEY (predecessor_task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: task_handoffs task_handoffs_successor_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_handoffs
    ADD CONSTRAINT task_handoffs_successor_task_id_fkey FOREIGN KEY (successor_task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: task_handoffs task_handoffs_to_executor_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_handoffs
    ADD CONSTRAINT task_handoffs_to_executor_id_fkey FOREIGN KEY (to_executor_id) REFERENCES public.executors(id) ON DELETE SET NULL;

--
-- Name: task_participants task_participants_identity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_participants
    ADD CONSTRAINT task_participants_identity_id_fkey FOREIGN KEY (identity_id) REFERENCES public.identities(id) ON DELETE CASCADE;

--
-- Name: task_participants task_participants_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_participants
    ADD CONSTRAINT task_participants_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: task_participants task_participants_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_participants
    ADD CONSTRAINT task_participants_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: task_recurrences task_recurrences_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_recurrences
    ADD CONSTRAINT task_recurrences_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: task_relations task_relations_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_relations
    ADD CONSTRAINT task_relations_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: task_relations task_relations_task_a_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_relations
    ADD CONSTRAINT task_relations_task_a_id_fkey FOREIGN KEY (task_a_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: task_relations task_relations_task_b_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_relations
    ADD CONSTRAINT task_relations_task_b_id_fkey FOREIGN KEY (task_b_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: task_reminders task_reminders_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_reminders
    ADD CONSTRAINT task_reminders_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: task_reminders task_reminders_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_reminders
    ADD CONSTRAINT task_reminders_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: task_tags task_tags_tag_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_tags
    ADD CONSTRAINT task_tags_tag_id_fkey FOREIGN KEY (tag_id) REFERENCES public.tags(id) ON DELETE CASCADE;

--
-- Name: task_tags task_tags_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task_tags
    ADD CONSTRAINT task_tags_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: tasks tasks_assignee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_assignee_id_fkey FOREIGN KEY (assignee_id) REFERENCES public.identities(id) ON DELETE SET NULL;

--
-- Name: tasks tasks_budget_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_budget_id_fkey FOREIGN KEY (budget_id) REFERENCES public.budgets(id) ON DELETE SET NULL;

--
-- Name: tasks tasks_created_by_identity_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_created_by_identity_id_fkey FOREIGN KEY (created_by_identity_id) REFERENCES public.identities(id) ON DELETE SET NULL;

--
-- Name: tasks tasks_created_by_token_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_created_by_token_id_fkey FOREIGN KEY (created_by_token_id) REFERENCES public.agent_tokens(id) ON DELETE SET NULL;

--
-- Name: tasks tasks_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.users(id) ON DELETE RESTRICT;

--
-- Name: tasks tasks_parent_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_parent_task_id_fkey FOREIGN KEY (parent_task_id) REFERENCES public.tasks(id) ON DELETE SET NULL;

--
-- Name: tasks tasks_state_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_state_id_fkey FOREIGN KEY (state_id) REFERENCES public.workflow_states(id) ON DELETE RESTRICT;

--
-- Name: telegram_assistant_jobs telegram_assistant_jobs_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_assistant_jobs
    ADD CONSTRAINT telegram_assistant_jobs_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: telegram_assistant_jobs telegram_assistant_jobs_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_assistant_jobs
    ADD CONSTRAINT telegram_assistant_jobs_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: telegram_link_codes telegram_link_codes_org_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_link_codes
    ADD CONSTRAINT telegram_link_codes_org_id_fkey FOREIGN KEY (org_id) REFERENCES public.organizations(id) ON DELETE CASCADE;

--
-- Name: telegram_link_codes telegram_link_codes_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_link_codes
    ADD CONSTRAINT telegram_link_codes_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: telegram_links telegram_links_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.telegram_links
    ADD CONSTRAINT telegram_links_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: time_entries time_entries_note_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entries
    ADD CONSTRAINT time_entries_note_id_fkey FOREIGN KEY (note_id) REFERENCES public.notes(id) ON DELETE SET NULL;

--
-- Name: time_entries time_entries_task_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entries
    ADD CONSTRAINT time_entries_task_id_fkey FOREIGN KEY (task_id) REFERENCES public.tasks(id) ON DELETE CASCADE;

--
-- Name: time_entries time_entries_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.time_entries
    ADD CONSTRAINT time_entries_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: user_calendar user_calendar_calendar_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_calendar
    ADD CONSTRAINT user_calendar_calendar_id_fkey FOREIGN KEY (calendar_id) REFERENCES public.working_calendars(id) ON DELETE CASCADE;

--
-- Name: user_calendar user_calendar_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_calendar
    ADD CONSTRAINT user_calendar_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;

--
-- Name: workflow_states workflow_states_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_states
    ADD CONSTRAINT workflow_states_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.workflow_defs(id) ON DELETE CASCADE;

--
-- Name: workflow_transitions workflow_transitions_from_state_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_transitions
    ADD CONSTRAINT workflow_transitions_from_state_id_fkey FOREIGN KEY (from_state_id) REFERENCES public.workflow_states(id) ON DELETE CASCADE;

--
-- Name: workflow_transitions workflow_transitions_to_state_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_transitions
    ADD CONSTRAINT workflow_transitions_to_state_id_fkey FOREIGN KEY (to_state_id) REFERENCES public.workflow_states(id) ON DELETE CASCADE;

--
-- Name: workflow_transitions workflow_transitions_workflow_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.workflow_transitions
    ADD CONSTRAINT workflow_transitions_workflow_id_fkey FOREIGN KEY (workflow_id) REFERENCES public.workflow_defs(id) ON DELETE CASCADE;

--
-- Name: activity_log; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.activity_log ENABLE ROW LEVEL SECURITY;

--
-- Name: adjudication_steps; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.adjudication_steps ENABLE ROW LEVEL SECURITY;

--
-- Name: adjudications; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.adjudications ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_runs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent_runs ENABLE ROW LEVEL SECURITY;

--
-- Name: agent_tokens; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.agent_tokens ENABLE ROW LEVEL SECURITY;

--
-- Name: ai_assistants; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.ai_assistants ENABLE ROW LEVEL SECURITY;

--
-- Name: attachments; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.attachments ENABLE ROW LEVEL SECURITY;

--
-- Name: billing_config; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.billing_config ENABLE ROW LEVEL SECURITY;

--
-- Name: blob_sources; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.blob_sources ENABLE ROW LEVEL SECURITY;

--
-- Name: budgets; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.budgets ENABLE ROW LEVEL SECURITY;

--
-- Name: calendar_holidays; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.calendar_holidays ENABLE ROW LEVEL SECURITY;

--
-- Name: client_profile; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.client_profile ENABLE ROW LEVEL SECURITY;

--
-- Name: comments; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.comments ENABLE ROW LEVEL SECURITY;

--
-- Name: credit_ledger; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.credit_ledger ENABLE ROW LEVEL SECURITY;

--
-- Name: dispatch_requests; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.dispatch_requests ENABLE ROW LEVEL SECURITY;

--
-- Name: email_accounts; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.email_accounts ENABLE ROW LEVEL SECURITY;

--
-- Name: email_messages; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.email_messages ENABLE ROW LEVEL SECURITY;

--
-- Name: executors; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.executors ENABLE ROW LEVEL SECURITY;

--
-- Name: google_calendar_subscriptions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.google_calendar_subscriptions ENABLE ROW LEVEL SECURITY;

--
-- Name: identities; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.identities ENABLE ROW LEVEL SECURITY;

--
-- Name: invoice_counters; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.invoice_counters ENABLE ROW LEVEL SECURITY;

--
-- Name: invoice_lines; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.invoice_lines ENABLE ROW LEVEL SECURITY;

--
-- Name: invoices; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.invoices ENABLE ROW LEVEL SECURITY;

--
-- Name: issuer_profiles; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.issuer_profiles ENABLE ROW LEVEL SECURITY;

--
-- Name: memberships; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.memberships ENABLE ROW LEVEL SECURITY;

--
-- Name: memory_blob_tags; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.memory_blob_tags ENABLE ROW LEVEL SECURITY;

--
-- Name: memory_blobs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.memory_blobs ENABLE ROW LEVEL SECURITY;

--
-- Name: note_note_link; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.note_note_link ENABLE ROW LEVEL SECURITY;

--
-- Name: note_tags; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.note_tags ENABLE ROW LEVEL SECURITY;

--
-- Name: note_task_link; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.note_task_link ENABLE ROW LEVEL SECURITY;

--
-- Name: note_turns; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.note_turns ENABLE ROW LEVEL SECURITY;

--
-- Name: notes; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.notes ENABLE ROW LEVEL SECURITY;

--
-- Name: notification_prefs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.notification_prefs ENABLE ROW LEVEL SECURITY;

--
-- Name: notifications; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.notifications ENABLE ROW LEVEL SECURITY;

--
-- Name: organizations; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.organizations ENABLE ROW LEVEL SECURITY;

--
-- Name: activity_log p_activity_log; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_activity_log ON public.activity_log USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: adjudication_steps p_adjudication_steps; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_adjudication_steps ON public.adjudication_steps USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: adjudications p_adjudications; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_adjudications ON public.adjudications USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: agent_runs p_agent_runs; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_agent_runs ON public.agent_runs USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: agent_tokens p_agent_tokens; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_agent_tokens ON public.agent_tokens USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: ai_assistants p_ai_assistants; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_ai_assistants ON public.ai_assistants USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: attachments p_attachments; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_attachments ON public.attachments USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: billing_config p_billing_config; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_billing_config ON public.billing_config USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: blob_sources p_blob_sources; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_blob_sources ON public.blob_sources USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: budgets p_budgets; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_budgets ON public.budgets USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: calendar_holidays p_calendar_holidays; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_calendar_holidays ON public.calendar_holidays USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: client_profile p_client_profile; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_client_profile ON public.client_profile USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: comments p_comments; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_comments ON public.comments USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: credit_ledger p_credit_ledger; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_credit_ledger ON public.credit_ledger USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: dispatch_requests p_dispatch_requests; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_dispatch_requests ON public.dispatch_requests USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: email_accounts p_email_accounts; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_email_accounts ON public.email_accounts USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: email_messages p_email_messages; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_email_messages ON public.email_messages USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: executors p_executors; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_executors ON public.executors USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: google_calendar_subscriptions p_google_calendar_subscriptions; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_google_calendar_subscriptions ON public.google_calendar_subscriptions USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: identities p_identities; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_identities ON public.identities USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: invoice_counters p_invoice_counters; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_invoice_counters ON public.invoice_counters USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: invoice_lines p_invoice_lines; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_invoice_lines ON public.invoice_lines USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: invoices p_invoices; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_invoices ON public.invoices USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: issuer_profiles p_issuer_profiles; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_issuer_profiles ON public.issuer_profiles USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: memberships p_memberships; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_memberships ON public.memberships USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: memberships p_memberships_self_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_memberships_self_read ON public.memberships FOR SELECT USING ((user_id = (NULLIF(current_setting('app.current_user'::text, true), ''::text))::uuid));

--
-- Name: memory_blob_tags p_memory_blob_tags; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_memory_blob_tags ON public.memory_blob_tags USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: memory_blobs p_memory_blobs; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_memory_blobs ON public.memory_blobs USING (((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid) AND ((NULLIF(current_setting('app.current_project'::text, true), ''::text) IS NULL) OR (project_id = (NULLIF(current_setting('app.current_project'::text, true), ''::text))::uuid)))) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: note_note_link p_note_note_link; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_note_note_link ON public.note_note_link USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: note_tags p_note_tags; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_note_tags ON public.note_tags USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: note_task_link p_note_task_link; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_note_task_link ON public.note_task_link USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: note_turns p_note_turns; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_note_turns ON public.note_turns USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: notes p_notes; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_notes ON public.notes USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: notification_prefs p_notification_prefs; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_notification_prefs ON public.notification_prefs USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: notifications p_notifications; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_notifications ON public.notifications USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: organizations p_organizations; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_organizations ON public.organizations USING ((id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: organizations p_organizations_self_read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_organizations_self_read ON public.organizations FOR SELECT USING ((id IN ( SELECT m.org_id
   FROM public.memberships m
  WHERE (m.user_id = (NULLIF(current_setting('app.current_user'::text, true), ''::text))::uuid))));

--
-- Name: project_profile p_project_profile; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_project_profile ON public.project_profile USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: rate_cards p_rate_cards; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_rate_cards ON public.rate_cards USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: received_invoices p_received_invoices; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_received_invoices ON public.received_invoices USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: schedule p_schedule; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_schedule ON public.schedule USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: sdi_mandates p_sdi_mandates; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_sdi_mandates ON public.sdi_mandates USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: sdi_transmission_counters p_sdi_transmission_counters; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_sdi_transmission_counters ON public.sdi_transmission_counters USING (true) WITH CHECK (true);

--
-- Name: storage_rates p_storage_rates; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_storage_rates ON public.storage_rates USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: tag_scopes p_tag_scopes; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_tag_scopes ON public.tag_scopes USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: tags p_tags; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_tags ON public.tags USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: task_checklist_items p_task_checklist_items; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_task_checklist_items ON public.task_checklist_items USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: task_collaborators p_task_collaborators; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_task_collaborators ON public.task_collaborators USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: task_dependencies p_task_dependencies; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_task_dependencies ON public.task_dependencies USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: task_handoffs p_task_handoffs; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_task_handoffs ON public.task_handoffs USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: task_participants p_task_participants; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_task_participants ON public.task_participants USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: task_recurrences p_task_recurrences; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_task_recurrences ON public.task_recurrences USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: task_relations p_task_relations; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_task_relations ON public.task_relations USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: task_reminders p_task_reminders; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_task_reminders ON public.task_reminders USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: task_tags p_task_tags; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_task_tags ON public.task_tags USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: tasks p_tasks; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_tasks ON public.tasks USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: telegram_assistant_jobs p_telegram_assistant_jobs; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_telegram_assistant_jobs ON public.telegram_assistant_jobs USING (true) WITH CHECK (true);

--
-- Name: telegram_conversations p_telegram_conversations; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_telegram_conversations ON public.telegram_conversations USING (true) WITH CHECK (true);

--
-- Name: telegram_link_codes p_telegram_link_codes; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_telegram_link_codes ON public.telegram_link_codes USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: telegram_links p_telegram_links_self; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_telegram_links_self ON public.telegram_links USING ((user_id = (NULLIF(current_setting('app.current_user'::text, true), ''::text))::uuid)) WITH CHECK ((user_id = (NULLIF(current_setting('app.current_user'::text, true), ''::text))::uuid));

--
-- Name: telegram_updates p_telegram_updates; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_telegram_updates ON public.telegram_updates USING (true) WITH CHECK (true);

--
-- Name: time_entries p_time_entries; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_time_entries ON public.time_entries USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: usage_record p_usage_record; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_usage_record ON public.usage_record USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: user_calendar p_user_calendar; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_user_calendar ON public.user_calendar USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: wallet p_wallet; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_wallet ON public.wallet USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: workflow_defs p_workflow_defs; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_workflow_defs ON public.workflow_defs USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: workflow_states p_workflow_states; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_workflow_states ON public.workflow_states USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: workflow_transitions p_workflow_transitions; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_workflow_transitions ON public.workflow_transitions USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: working_calendars p_working_calendars; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY p_working_calendars ON public.working_calendars USING ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid)) WITH CHECK ((org_id = (NULLIF(current_setting('app.current_org'::text, true), ''::text))::uuid));

--
-- Name: project_profile; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.project_profile ENABLE ROW LEVEL SECURITY;

--
-- Name: rate_cards; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.rate_cards ENABLE ROW LEVEL SECURITY;

--
-- Name: received_invoices; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.received_invoices ENABLE ROW LEVEL SECURITY;

--
-- Name: schedule; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.schedule ENABLE ROW LEVEL SECURITY;

--
-- Name: sdi_mandates; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.sdi_mandates ENABLE ROW LEVEL SECURITY;

--
-- Name: sdi_transmission_counters; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.sdi_transmission_counters ENABLE ROW LEVEL SECURITY;

--
-- Name: storage_rates; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.storage_rates ENABLE ROW LEVEL SECURITY;

--
-- Name: tag_scopes; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.tag_scopes ENABLE ROW LEVEL SECURITY;

--
-- Name: tags; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.tags ENABLE ROW LEVEL SECURITY;

--
-- Name: task_checklist_items; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.task_checklist_items ENABLE ROW LEVEL SECURITY;

--
-- Name: task_collaborators; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.task_collaborators ENABLE ROW LEVEL SECURITY;

--
-- Name: task_dependencies; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.task_dependencies ENABLE ROW LEVEL SECURITY;

--
-- Name: task_handoffs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.task_handoffs ENABLE ROW LEVEL SECURITY;

--
-- Name: task_participants; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.task_participants ENABLE ROW LEVEL SECURITY;

--
-- Name: task_recurrences; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.task_recurrences ENABLE ROW LEVEL SECURITY;

--
-- Name: task_relations; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.task_relations ENABLE ROW LEVEL SECURITY;

--
-- Name: task_reminders; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.task_reminders ENABLE ROW LEVEL SECURITY;

--
-- Name: task_tags; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.task_tags ENABLE ROW LEVEL SECURITY;

--
-- Name: tasks; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.tasks ENABLE ROW LEVEL SECURITY;

--
-- Name: telegram_assistant_jobs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.telegram_assistant_jobs ENABLE ROW LEVEL SECURITY;

--
-- Name: telegram_conversations; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.telegram_conversations ENABLE ROW LEVEL SECURITY;

--
-- Name: telegram_link_codes; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.telegram_link_codes ENABLE ROW LEVEL SECURITY;

--
-- Name: telegram_links; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.telegram_links ENABLE ROW LEVEL SECURITY;

--
-- Name: telegram_updates; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.telegram_updates ENABLE ROW LEVEL SECURITY;

--
-- Name: time_entries; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.time_entries ENABLE ROW LEVEL SECURITY;

--
-- Name: usage_record; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.usage_record ENABLE ROW LEVEL SECURITY;

--
-- Name: user_calendar; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.user_calendar ENABLE ROW LEVEL SECURITY;

--
-- Name: wallet; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.wallet ENABLE ROW LEVEL SECURITY;

--
-- Name: workflow_defs; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.workflow_defs ENABLE ROW LEVEL SECURITY;

--
-- Name: workflow_states; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.workflow_states ENABLE ROW LEVEL SECURITY;

--
-- Name: workflow_transitions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.workflow_transitions ENABLE ROW LEVEL SECURITY;

--
-- Name: working_calendars; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.working_calendars ENABLE ROW LEVEL SECURITY;

--
-- Name: SCHEMA public; Type: ACL; Schema: -; Owner: -
--

GRANT USAGE ON SCHEMA public TO flow_app;

--
-- Name: FUNCTION add_org_member(p_org uuid, p_actor uuid, p_email text, p_role text); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.add_org_member(p_org uuid, p_actor uuid, p_email text, p_role text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.add_org_member(p_org uuid, p_actor uuid, p_email text, p_role text) TO flow_app;

--
-- Name: FUNCTION authenticate_agent_token(p_hash bytea, OUT out_token_id uuid, OUT out_user_id uuid, OUT out_org_id uuid, OUT out_scope text, OUT out_assistant_id uuid, OUT out_assistant_scope jsonb, OUT out_assistant_active boolean); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.authenticate_agent_token(p_hash bytea, OUT out_token_id uuid, OUT out_user_id uuid, OUT out_org_id uuid, OUT out_scope text, OUT out_assistant_id uuid, OUT out_assistant_scope jsonb, OUT out_assistant_active boolean) FROM PUBLIC;
GRANT ALL ON FUNCTION public.authenticate_agent_token(p_hash bytea, OUT out_token_id uuid, OUT out_user_id uuid, OUT out_org_id uuid, OUT out_scope text, OUT out_assistant_id uuid, OUT out_assistant_scope jsonb, OUT out_assistant_active boolean) TO flow_app;

--
-- Name: FUNCTION consume_telegram_link_code(p_code text, p_chat_id bigint, p_chat_username text, OUT out_user_id uuid, OUT out_org_id uuid); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.consume_telegram_link_code(p_code text, p_chat_id bigint, p_chat_username text, OUT out_user_id uuid, OUT out_org_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.consume_telegram_link_code(p_code text, p_chat_id bigint, p_chat_username text, OUT out_user_id uuid, OUT out_org_id uuid) TO flow_app;

--
-- Name: FUNCTION create_default_calendar(p_org uuid); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.create_default_calendar(p_org uuid) FROM PUBLIC;

--
-- Name: FUNCTION create_default_workflow(p_org uuid); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.create_default_workflow(p_org uuid) FROM PUBLIC;

--
-- Name: FUNCTION delete_organization(p_org uuid, p_user uuid); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.delete_organization(p_org uuid, p_user uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.delete_organization(p_org uuid, p_user uuid) TO flow_app;

--
-- Name: FUNCTION list_org_members(p_org uuid, p_user uuid); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.list_org_members(p_org uuid, p_user uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.list_org_members(p_org uuid, p_user uuid) TO flow_app;

--
-- Name: FUNCTION list_user_organizations(p_user_id uuid); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.list_user_organizations(p_user_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.list_user_organizations(p_user_id uuid) TO flow_app;

--
-- Name: FUNCTION oauth_token_diag(p_hash bytea); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.oauth_token_diag(p_hash bytea) FROM PUBLIC;
GRANT ALL ON FUNCTION public.oauth_token_diag(p_hash bytea) TO flow_app;

--
-- Name: FUNCTION provision_organization(p_name text, p_user_id uuid); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.provision_organization(p_name text, p_user_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.provision_organization(p_name text, p_user_id uuid) TO flow_app;

--
-- Name: FUNCTION remove_org_member(p_org uuid, p_actor uuid, p_target uuid); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.remove_org_member(p_org uuid, p_actor uuid, p_target uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.remove_org_member(p_org uuid, p_actor uuid, p_target uuid) TO flow_app;

--
-- Name: FUNCTION resolve_telegram_chat(p_chat_id bigint, OUT out_user_id uuid, OUT out_default_org_id uuid); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.resolve_telegram_chat(p_chat_id bigint, OUT out_user_id uuid, OUT out_default_org_id uuid) FROM PUBLIC;
GRANT ALL ON FUNCTION public.resolve_telegram_chat(p_chat_id bigint, OUT out_user_id uuid, OUT out_default_org_id uuid) TO flow_app;

--
-- Name: FUNCTION sdi_resolve_invoice_org(p_identificativo text); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.sdi_resolve_invoice_org(p_identificativo text) TO flow_app;

--
-- Name: FUNCTION sdi_resolve_recipient_org(p_codice text); Type: ACL; Schema: public; Owner: -
--

GRANT ALL ON FUNCTION public.sdi_resolve_recipient_org(p_codice text) TO flow_app;

--
-- Name: FUNCTION set_member_role(p_org uuid, p_actor uuid, p_target uuid, p_role text); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.set_member_role(p_org uuid, p_actor uuid, p_target uuid, p_role text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.set_member_role(p_org uuid, p_actor uuid, p_target uuid, p_role text) TO flow_app;

--
-- Name: FUNCTION set_organization_status(p_org uuid, p_user uuid, p_status text); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.set_organization_status(p_org uuid, p_user uuid, p_status text) FROM PUBLIC;
GRANT ALL ON FUNCTION public.set_organization_status(p_org uuid, p_user uuid, p_status text) TO flow_app;

--
-- Name: FUNCTION sync_task_assignee_participant(); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.sync_task_assignee_participant() FROM PUBLIC;

--
-- Name: FUNCTION sync_task_participants_window(); Type: ACL; Schema: public; Owner: -
--

REVOKE ALL ON FUNCTION public.sync_task_participants_window() FROM PUBLIC;

--
-- Name: TABLE activity_log; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.activity_log TO flow_app;

--
-- Name: TABLE adjudication_steps; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.adjudication_steps TO flow_app;

--
-- Name: TABLE adjudications; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.adjudications TO flow_app;

--
-- Name: TABLE agent_runs; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agent_runs TO flow_app;

--
-- Name: TABLE agent_tokens; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.agent_tokens TO flow_app;

--
-- Name: TABLE ai_assistants; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.ai_assistants TO flow_app;

--
-- Name: TABLE attachments; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.attachments TO flow_app;

--
-- Name: TABLE billing_config; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,UPDATE ON TABLE public.billing_config TO flow_app;

--
-- Name: TABLE blob_sources; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.blob_sources TO flow_app;

--
-- Name: TABLE budgets; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.budgets TO flow_app;

--
-- Name: TABLE calendar_holidays; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.calendar_holidays TO flow_app;

--
-- Name: TABLE client_profile; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.client_profile TO flow_app;

--
-- Name: TABLE comments; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.comments TO flow_app;

--
-- Name: TABLE credit_ledger; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.credit_ledger TO flow_app;

--
-- Name: TABLE dispatch_requests; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.dispatch_requests TO flow_app;

--
-- Name: TABLE email_accounts; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.email_accounts TO flow_app;

--
-- Name: TABLE email_messages; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.email_messages TO flow_app;

--
-- Name: TABLE email_verification_tokens; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.email_verification_tokens TO flow_app;

--
-- Name: TABLE executors; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.executors TO flow_app;

--
-- Name: TABLE google_calendar_subscriptions; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.google_calendar_subscriptions TO flow_app;

--
-- Name: TABLE identities; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.identities TO flow_app;

--
-- Name: TABLE invoice_counters; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.invoice_counters TO flow_app;

--
-- Name: TABLE invoice_lines; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.invoice_lines TO flow_app;

--
-- Name: TABLE invoices; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.invoices TO flow_app;

--
-- Name: TABLE issuer_profiles; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.issuer_profiles TO flow_app;

--
-- Name: TABLE memberships; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.memberships TO flow_app;

--
-- Name: TABLE memory_blob_tags; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.memory_blob_tags TO flow_app;

--
-- Name: TABLE memory_blobs; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.memory_blobs TO flow_app;

--
-- Name: TABLE note_note_link; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.note_note_link TO flow_app;

--
-- Name: TABLE note_tags; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.note_tags TO flow_app;

--
-- Name: TABLE note_task_link; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.note_task_link TO flow_app;

--
-- Name: TABLE note_turns; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.note_turns TO flow_app;

--
-- Name: TABLE notes; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notes TO flow_app;

--
-- Name: TABLE notification_prefs; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notification_prefs TO flow_app;

--
-- Name: TABLE notifications; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.notifications TO flow_app;

--
-- Name: TABLE oauth_codes; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE ON TABLE public.oauth_codes TO flow_app;

--
-- Name: TABLE organizations; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.organizations TO flow_app;

--
-- Name: TABLE password_reset_tokens; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.password_reset_tokens TO flow_app;

--
-- Name: TABLE project_profile; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.project_profile TO flow_app;

--
-- Name: TABLE rate_cards; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.rate_cards TO flow_app;

--
-- Name: TABLE received_invoices; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.received_invoices TO flow_app;

--
-- Name: TABLE revoked_tokens; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.revoked_tokens TO flow_app;

--
-- Name: TABLE schedule; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.schedule TO flow_app;

--
-- Name: TABLE sdi_mandates; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.sdi_mandates TO flow_app;

--
-- Name: TABLE sdi_transmission_counters; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.sdi_transmission_counters TO flow_app;

--
-- Name: TABLE storage_rates; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.storage_rates TO flow_app;

--
-- Name: TABLE tag_scopes; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tag_scopes TO flow_app;

--
-- Name: TABLE tags; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tags TO flow_app;

--
-- Name: TABLE task_checklist_items; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_checklist_items TO flow_app;

--
-- Name: TABLE task_collaborators; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_collaborators TO flow_app;

--
-- Name: TABLE task_dependencies; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_dependencies TO flow_app;

--
-- Name: TABLE task_handoffs; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_handoffs TO flow_app;

--
-- Name: TABLE task_participants; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_participants TO flow_app;

--
-- Name: TABLE task_recurrences; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_recurrences TO flow_app;

--
-- Name: TABLE task_relations; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_relations TO flow_app;

--
-- Name: TABLE task_reminders; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_reminders TO flow_app;

--
-- Name: TABLE task_tags; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.task_tags TO flow_app;

--
-- Name: TABLE tasks; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.tasks TO flow_app;

--
-- Name: TABLE telegram_assistant_jobs; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.telegram_assistant_jobs TO flow_app;

--
-- Name: TABLE telegram_conversations; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.telegram_conversations TO flow_app;

--
-- Name: TABLE telegram_link_codes; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.telegram_link_codes TO flow_app;

--
-- Name: TABLE telegram_links; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.telegram_links TO flow_app;

--
-- Name: TABLE telegram_updates; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.telegram_updates TO flow_app;

--
-- Name: TABLE time_entries; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.time_entries TO flow_app;

--
-- Name: TABLE usage_record; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT ON TABLE public.usage_record TO flow_app;

--
-- Name: TABLE user_calendar; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.user_calendar TO flow_app;

--
-- Name: TABLE users; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.users TO flow_app;

--
-- Name: TABLE wallet; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,UPDATE ON TABLE public.wallet TO flow_app;

--
-- Name: TABLE workflow_defs; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.workflow_defs TO flow_app;

--
-- Name: TABLE workflow_states; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.workflow_states TO flow_app;

--
-- Name: TABLE workflow_transitions; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.workflow_transitions TO flow_app;

--
-- Name: TABLE working_calendars; Type: ACL; Schema: public; Owner: -
--

GRANT SELECT,INSERT,DELETE,UPDATE ON TABLE public.working_calendars TO flow_app;

--
-- PostgreSQL database dump complete
--

