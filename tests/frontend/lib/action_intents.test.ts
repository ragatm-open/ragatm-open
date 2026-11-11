// Corpus commun front/back (shared/contracts/action_intents/summarize.json).
// Côté frontend : parseSummaryIntent (pré-détection UX, sans garde-fous) doit
// décider `frontend_is_summary`. Le pendant Python
// (backend/tests/actions/test_fixtures_summarize.py) vérifie `backend_action`.
import { describe, it, expect } from "vitest";
import { parseSummaryIntent } from "@/app/chat/_lib/summaryIntent";
import fixtures from "../../../shared/contracts/action_intents/summarize.json";

type Case = {
  text: string;
  category: "positive" | "negative" | "ambiguous";
  frontend_is_summary: boolean;
  backend_action: string;
  note: string;
};

describe("action_intents (frontend parseSummaryIntent vs corpus commun)", () => {
  for (const c of fixtures.cases as Case[]) {
    it(`[${c.category}] ${c.text}`, () => {
      expect(parseSummaryIntent(c.text).isSummary).toBe(c.frontend_is_summary);
    });
  }
});
