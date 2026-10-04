import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, Navigate, useLocation } from "react-router";

import { ApiResponseError, fetchCandidateWorkspace, type CandidateWorkspace } from "./api/client";
import { CandidateMemberEvidence } from "./CandidateReleaseEvidence";
import { ReportRecord as Record } from "./ReportRecord";

type Release = CandidateWorkspace["releases"][number];

function absoluteTime(value: string | null | undefined): string {
  return value
    ? `${new Intl.DateTimeFormat("en-CA", {
        timeZone: "Asia/Shanghai",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        hourCycle: "h23"
      }).format(new Date(value))} Asia/Shanghai`
    : "Unavailable";
}

function observedRelease(release: Release, now: number): Release {
  return ["CURRENT", "WAITING_MARKET"].includes(release.status) &&
    release.valid_through &&
    now > Date.parse(release.valid_through)
    ? { ...release, status: "EXPIRED", status_reasons: ["CANDIDATE_WINDOW_ENDED"] }
    : release;
}

function useExpiryClock(releases: Release[]) {
  const [now, setNow] = useState(() => Date.now());
  const nextExpiry = Math.min(
    ...releases
      .filter((item) => ["CURRENT", "WAITING_MARKET"].includes(item.status) && item.valid_through)
      .map((item) => Date.parse(item.valid_through!) + 1)
      .filter((deadline) => deadline > now)
  );
  useEffect(() => {
    if (!Number.isFinite(nextExpiry)) return;
    const timer = setTimeout(
      () => setNow(Date.now()),
      Math.min(Math.max(0, nextExpiry - Date.now()), 2147483647)
    );
    return () => clearTimeout(timer);
  }, [nextExpiry, now]);
  return now;
}

function ReleaseHeader({ release }: { release: Release }) {
  const members = release.release.members;
  const candidates = members.filter((member) => member.candidate).length;
  const rejected = members.filter(
    (member) => !member.candidate && member.risk_status === "REJECTED"
  ).length;
  const failed = members.filter(
    (member) => !member.candidate && member.risk_status === "FAILED"
  ).length;
  return (
    <>
      <h2>Candidate release · {release.plan_month}</h2>
      <CandidateReleaseStatus release={release} />
      <dl className="record-list">
        <Record
          label="Frozen pool count"
          value={String(release.frozen_pool_count ?? "Unavailable")}
        />
        <Record
          label="Research completed"
          value={String(release.research_completed_count ?? "Unavailable")}
        />
        <Record label="Candidate count" value={String(candidates)} />
        <Record label="Rejected count" value={String(rejected)} />
        <Record
          label="Abstained count"
          value={String(members.length - candidates - rejected - failed)}
        />
        <Record label="Failed count" value={String(failed)} />
        <Record label="Batch result reasons" value={release.release.reasons.join(", ") || "None"} />
        <Record label="Knowledge cutoff" value={absoluteTime(release.knowledge_cutoff)} />
        <Record label="Generated" value={absoluteTime(release.generated_at)} />
        <Record label="Committed" value={absoluteTime(release.committed_at)} />
        <Record label="Published" value={absoluteTime(release.published_at)} />
        <Record label="Valid from" value={absoluteTime(release.valid_from)} />
        <Record label="Expires at" value={absoluteTime(release.valid_through)} />
        <Record
          label="Five market sessions"
          value={release.release.valid_market_dates.join(", ") || "Unavailable"}
        />
        <Record
          label="Current synthetic qualification"
          value={release.current_qualification_status ?? "UNAVAILABLE"}
        />
        <Record
          label="Synthetic qualification at publication"
          value={release.release.qualification?.status ?? "NOT_OBTAINED"}
        />
        <Record
          label="Qualification valid through"
          value={absoluteTime(release.release.qualification?.valid_through)}
        />
        <Record
          label="Current status reasons"
          value={release.status_reasons.join(", ") || "None"}
        />
      </dl>
      <h3>Frozen evidence freshness</h3>
      <ul>
        {release.release.members.map((member) => (
          <li key={member.research_id}>
            {member.security_id}: {member.evidence_freshness}
          </li>
        ))}
      </ul>
      <h3>Reminder history</h3>
      {release.reminders?.length ? (
        <ul>
          {release.reminders.map((reminder) => (
            <li key={reminder.attempt_id}>
              {reminder.kind} · {reminder.status} · {absoluteTime(reminder.recorded_at)}
              {reminder.channels.map((channel) => ` · ${channel.role}: ${channel.result}`).join("")}
            </li>
          ))}
        </ul>
      ) : (
        <p>No recorded reminder attempts.</p>
      )}
      {release.superseded_by_report_id && (
        <p>
          <Link to={`/candidates/releases/${release.superseded_by_report_id}`}>
            Read replacement release
          </Link>
        </p>
      )}
      {release.corrects_report_id && (
        <p>
          <Link to={`/candidates/releases/${release.corrects_report_id}`}>
            Read original release
          </Link>
        </p>
      )}
      <p>
        <Link to={`/reports/${release.report_version_id}`}>Read full report and event history</Link>
      </p>
    </>
  );
}

export function CandidateReleaseStatus({ release }: { release?: Release }) {
  const now = useExpiryClock(release ? [release] : []);
  if (release) release = observedRelease(release, now);
  if (!release)
    return (
      <p role="status">
        Current candidate eligibility unavailable. This report is a saved historical snapshot.
      </p>
    );
  return (
    <>
      <p className="outcome-code">
        {release.status} · {release.release.disposition}
      </p>
      {!["CURRENT", "WAITING_MARKET", "RESULT"].includes(release.status) && (
        <p role="status">
          Historical result. This release cannot be used as a current candidate recommendation.
        </p>
      )}
    </>
  );
}

export function CandidatePage() {
  const location = useLocation();
  const query = useQuery({
    queryKey: ["candidate-workspace", location.pathname],
    queryFn: fetchCandidateWorkspace,
    refetchOnWindowFocus: true,
    refetchInterval: 60000
  });
  const now = useExpiryClock(query.data?.releases ?? []);
  if (query.error instanceof ApiResponseError && query.error.status === 401) {
    return <Navigate replace to={`/sign-in?next=${encodeURIComponent(location.pathname)}`} />;
  }
  const parts = location.pathname.split("/");
  const releaseId = parts[2] === "releases" ? parts[3] : undefined;
  const detailId = parts[2] === "details" ? parts[3] : undefined;
  const data = query.data && {
    ...query.data,
    releases: query.data.releases.map((item) => observedRelease(item, now))
  };
  const detail = data?.details.find((item) => item.detail_id === detailId);
  const release = data?.releases.find(
    (item) => item.report_version_id === (releaseId ?? detail?.report_version_id)
  );
  const archive = parts[2] === "archive";
  const history = archive
    ? data?.releases
    : data?.releases.filter((item) => data.current_report_ids.includes(item.report_version_id));
  return (
    <section className="report-section" aria-label="Candidate workspace">
      <h2>Candidate workspace</h2>
      <p>Saved synthetic candidate results</p>
      <nav className="workspace-nav" aria-label="Candidate views">
        <Link to="/candidates">Current releases</Link>
        <Link to="/candidates/archive">Candidate history</Link>
      </nav>
      {query.isPending && <p aria-live="polite">Loading candidate results</p>}
      {query.isError && <p aria-live="polite">Candidate results unavailable</p>}
      {data && (releaseId || detailId) ? (
        release && (!detailId || detail) ? (
          <>
            <ReleaseHeader release={release} />
            {detail ? (
              <>
                <p>
                  Security status: {detail.member.candidate ? release.status : detail.status} ·{" "}
                  {detail.status_reasons?.join(", ")}
                </p>
                <CandidateMemberEvidence member={detail.member} />
              </>
            ) : (
              <>
                <h3>Security details</h3>
                {release.detail_ids.length === 0 && <p>No security decisions were published.</p>}
                <ul>
                  {release.detail_ids.map((id) => {
                    const member = data.details.find((item) => item.detail_id === id)?.member;
                    return (
                      <li key={id}>
                        <Link to={`/candidates/details/${id}`}>
                          {member?.security_id ?? "Security detail"}
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              </>
            )}
            {detail && (
              <p>
                <Link to={`/candidates/releases/${release.report_version_id}`}>
                  Read parent release
                </Link>
              </p>
            )}
          </>
        ) : (
          <p>Candidate result unavailable</p>
        )
      ) : (
        data && (
          <>
            <h3>{archive ? "Saved release history" : "Current monthly results"}</h3>
            {history?.length === 0 && <p>No saved candidate results.</p>}
            <ul>
              {history?.map((item) => (
                <li key={item.report_version_id}>
                  <Link to={`/candidates/releases/${item.report_version_id}`}>
                    {item.plan_month} · {item.release.disposition} · {item.status}
                  </Link>
                  <p>
                    Cutoff {absoluteTime(item.knowledge_cutoff)} · Expires{" "}
                    {absoluteTime(item.valid_through)}
                  </p>
                </li>
              ))}
            </ul>
          </>
        )
      )}
    </section>
  );
}
