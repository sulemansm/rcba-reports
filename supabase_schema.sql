-- ═══════════════════════════════════════════════════════════════════════════
-- RCBA Event Reporter — Supabase Schema
-- Run this in Supabase → SQL Editor before deploying.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── 1. reports ───────────────────────────────────────────────────────────────
create table if not exists reports (
    id                          bigint generated always as identity primary key,
    event_title                 text        not null,
    event_date                  date,
    submitted_by_email          text        not null,
    submitted_by_name           text,
    avenue                      text,
    drive_link                  text,
    status                      text        not null default 'submitted',
    is_late                     boolean     not null default false,
    submitted_at                timestamptz not null default now(),
    last_updated                timestamptz,
    -- attendance
    total_attendance            integer     not null default 0,
    member_names                text[]      not null default '{}',
    member_attendance_count     integer     not null default 0,
    guest_count                 integer     not null default 0,
    guest_names                 text,
    district_count              integer     not null default 0,
    district_names              text,
    ambassadorial_count         integer     not null default 0,
    ambassadorial_names         text,
    avenue_chairs               text[]      not null default '{}',
    -- review
    approved_by_email           text,
    approved_at                 timestamptz,
    approval_comments           text,
    rejection_message           text,
    reviewed_by                 text,
    reviewed_at                 text,
    review_comment              text,
    -- docx reference
    docx_file_id                bigint
);

-- ── 2. docx_files ────────────────────────────────────────────────────────────
create table if not exists docx_files (
    id           bigint generated always as identity primary key,
    report_id    bigint references reports(id) on delete cascade,
    filename     text,
    file_content text,   -- base64-encoded DOCX binary
    file_size    integer,
    created_at   timestamptz not null default now()
);

-- Add FK from reports → docx_files after both tables exist
do $$
begin
    if not exists (
        select 1 from information_schema.table_constraints
        where constraint_name = 'reports_docx_file_id_fkey'
    ) then
        alter table reports
            add constraint reports_docx_file_id_fkey
            foreign key (docx_file_id) references docx_files(id);
    end if;
end $$;

-- ── 3. members ───────────────────────────────────────────────────────────────
create table if not exists members (
    id         bigint generated always as identity primary key,
    name       text        not null unique,
    email      text        unique,
    role       text        not null default 'Member',
    added_date timestamptz not null default now()
);

-- ── 4. roles_config ──────────────────────────────────────────────────────────
create table if not exists roles_config (
    id          bigint generated always as identity primary key,
    email       text        not null unique,
    role        text        not null,   -- admin | secretariat | editor | director
    assigned_at timestamptz not null default now()
);

-- ── Row Level Security (optional but recommended) ─────────────────────────────
-- Disable RLS for service-key access (anon key with RLS off is simplest for now)
alter table reports     disable row level security;
alter table docx_files  disable row level security;
alter table members     disable row level security;
alter table roles_config disable row level security;

-- ── Indexes ───────────────────────────────────────────────────────────────────
create index if not exists idx_reports_email      on reports(submitted_by_email);
create index if not exists idx_reports_status     on reports(status);
create index if not exists idx_reports_submitted  on reports(submitted_at desc);
create index if not exists idx_docx_report_id     on docx_files(report_id);
create index if not exists idx_members_email      on members(email);
create index if not exists idx_roles_email        on roles_config(email);
