import { createContext, useContext, type Dispatch, type SetStateAction } from "react";
import type { CandidateWorkspaceCommand } from "./api/client";

export type CandidateSubmissions = Record<string, CandidateWorkspaceCommand>;
export const CandidateSubmissionContext = createContext<{
  requests: CandidateSubmissions;
  setRequests: Dispatch<SetStateAction<CandidateSubmissions>>;
} | null>(null);

export function useCandidateSubmission(planId: string) {
  const context = useContext(CandidateSubmissionContext);
  if (!context) throw new Error("Candidate submission owner is required");
  const { requests, setRequests } = context;
  return {
    pending: requests[planId] ?? null,
    anyPending: Object.keys(requests).length > 0,
    setPending(request: CandidateWorkspaceCommand | null) {
      setRequests((previous) => {
        const next = { ...previous };
        if (request) next[planId] = request;
        else delete next[planId];
        return next;
      });
    }
  };
}
