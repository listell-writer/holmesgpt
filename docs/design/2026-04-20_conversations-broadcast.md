# Conversation Worker — Broadcast-only Subscription

**Status:** Accepted
**Date:** 2026-04-20

## Summary

Holmes subscribes to new pending conversations via a Supabase Realtime
Broadcast channel. Postgres Changes is no longer used for conversation
submission notifications.

## Channel

Topic format:

```
holmes:submit:{account_id}:{cluster_id}
```

Parsing contract (for RLS and any server-side code):

- Split the topic on `:` with `max_splits=3` (yielding 4 parts).
- Part 1 (0-indexed: `0`) is the literal `holmes`.
- Part 2 (0-indexed: `1`) is the literal `submit`.
- Part 3 (0-indexed: `2`) is the `account_id`.
- Part 4 (0-indexed: `3`) is the entire `cluster_id` — **cluster_id may itself
  contain `:` characters** and must be preserved verbatim. Do not split further.

Example:

```
holmes:submit:acct_123:prod:us-east:1
                       └──── cluster_id ────┘
```

Event name on the channel: `pending_conversations`. Payload carries the
created conversation id; Holmes uses the event only as a wake-up signal and
re-reads the Conversations table on claim.

## Who may subscribe / broadcast

RLS on the Broadcast channel allows:

1. **Relay** (service-role) — can subscribe and broadcast for any
   `{account_id, cluster_id}`.
2. **Support** (staff users with support access) — can subscribe and
   broadcast for any `{account_id, cluster_id}`.
3. **Holmes** with account access — Holmes instances authenticated to the
   given account may subscribe and broadcast on that account's channels
   (any cluster under the account).
4. **Users with access to the specific cluster** — an end-user may
   subscribe / broadcast only for the `{account_id, cluster_id}` pair they
   have explicit cluster-level access to.

## Skipped (out of scope for this milestone)

- **Presence.** Holmes does not advertise presence on the Broadcast channel.
  Liveness is derived from the `Conversations.assignee` field and the
  claim-lock timeout in the RPCs.
- **Backfills.** On reconnect, Holmes does not replay missed broadcasts.
  The claim-loop's periodic poll (see
  `CONVERSATION_WORKER_POLL_INTERVAL_SECONDS_WITHOUT_REALTIME`) is the
  single safety net for any broadcast that was missed while disconnected
  or while the claim loop was busy.

## Schema — Realtime Broadcast RLS policy

The following policy is installed on the Supabase `realtime.messages` table
(used by Realtime Broadcast). Adjust the role / support checks to match your
deployment's conventions.

```sql
-- Enable RLS (no-op if already enabled).
alter table realtime.messages enable row level security;

-- Helper: extract the (account_id, cluster_id) pair from a topic.
-- Topic format: holmes:submit:{account_id}:{cluster_id}
-- account_id  = 3rd ':'-delimited segment
-- cluster_id  = full remaining suffix (may contain additional ':')
create or replace function public.parse_holmes_submit_topic(topic text)
returns table(account_id text, cluster_id text)
language sql
stable
as $$
  select
    split_part(topic, ':', 3) as account_id,
    substring(topic from length('holmes:submit:' || split_part(topic, ':', 3) || ':') + 1)
      as cluster_id
  where topic like 'holmes:submit:%:%';
$$;

-- SELECT / INSERT policy on realtime.messages for Holmes-submit topics.
drop policy if exists "holmes_submit_broadcast_access" on realtime.messages;
create policy "holmes_submit_broadcast_access"
on realtime.messages
for all
using (
  -- Only applies to holmes:submit:* topics; other topics fall through to
  -- their own policies.
  topic like 'holmes:submit:%:%'
  and exists (
    select 1
    from public.parse_holmes_submit_topic(topic) p
    where
      -- 1. Relay (service role) — full access.
      auth.role() = 'service_role'

      -- 2. Support users — staff flag on user metadata.
      or coalesce(
           (auth.jwt() -> 'app_metadata' ->> 'is_support')::boolean,
           false
         )

      -- 3. Holmes with account access — a row in HolmesStatuses for this
      --    account, authenticated with the account's Holmes JWT.
      or exists (
        select 1
        from public."HolmesStatuses" hs
        where hs.account_id = p.account_id
          and hs.cluster_id = p.cluster_id
          and auth.uid() = hs.holmes_user_id
      )

      -- 4. Users with access to the specific cluster.
      or exists (
        select 1
        from public."ClusterUsers" cu
        where cu.account_id = p.account_id
          and cu.cluster_id = p.cluster_id
          and cu.user_id = auth.uid()
      )
  )
)
with check (
  topic like 'holmes:submit:%:%'
  and exists (
    select 1
    from public.parse_holmes_submit_topic(topic) p
    where
      auth.role() = 'service_role'
      or coalesce(
           (auth.jwt() -> 'app_metadata' ->> 'is_support')::boolean,
           false
         )
      or exists (
        select 1
        from public."HolmesStatuses" hs
        where hs.account_id = p.account_id
          and hs.cluster_id = p.cluster_id
          and auth.uid() = hs.holmes_user_id
      )
      or exists (
        select 1
        from public."ClusterUsers" cu
        where cu.account_id = p.account_id
          and cu.cluster_id = p.cluster_id
          and cu.user_id = auth.uid()
      )
  )
);
```

Notes:

- `HolmesStatuses` and `ClusterUsers` are illustrative table names — replace
  with the actual access tables in your deployment.
- The `parse_holmes_submit_topic` helper enforces the "cluster_id may contain
  `:`" rule by extracting the full suffix rather than a single segment.
- Non-`holmes:submit:*` topics bypass this policy.
