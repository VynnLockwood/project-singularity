import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ScriptsTab } from "./ScriptsTab";

/** /api/schema/graph answers with bare table names in schema `master`. */
const graph = {
  tables: [
    { name: "district", comment: null, rowEstimate: 928, columns: [
      { name: "code", type: "character varying", nullable: false, primaryKey: true },
      { name: "name", type: "text", nullable: false, primaryKey: false }] },
    { name: "subdistrict", comment: null, rowEstimate: 0, columns: [
      { name: "code", type: "character varying", nullable: false, primaryKey: true },
      { name: "name", type: "text", nullable: false, primaryKey: false },
      { name: "postcode", type: "character varying", nullable: true, primaryKey: false },
      { name: "district_id", type: "integer", nullable: false, primaryKey: false }] },
  ],
  relations: [],
};

describe("ScriptsTab destination picker", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      const path = String(url);
      const body = path.includes("/schema/graph") ? graph : path.endsWith("/scripts") ? { scripts: [] } : {};
      return new Response(JSON.stringify(body), { status: 200, headers: { "content-type": "application/json" } });
    }));
  });
  afterEach(() => vi.unstubAllGlobals());

  function fill(label: string, value: string) {
    fireEvent.change(screen.getByLabelText(label), { target: { value } });
  }

  it("writes an external ref when resolving against an existing table", async () => {
    render(<ScriptsTab canEdit />);

    fill("Table 1 source table", "subdistrict_src");
    fireEvent.click(screen.getByLabelText("Table 1: load into an existing table"));
    await waitFor(() => expect(screen.getByLabelText("Table 1 existing table")).toBeInTheDocument());
    fill("Table 1 existing table", "subdistrict");

    fill("Table 1 field 1 source column", "code");
    fill("Table 1 field 1 destination field", "code");
    fireEvent.click(screen.getByRole("button", { name: "Add field to table 1" }));
    fill("Table 1 field 2 source column", "name");
    fill("Table 1 field 2 destination field", "name");
    fireEvent.click(screen.getByRole("button", { name: "Add field to table 1" }));
    fill("Table 1 field 3 source column", "postcode");
    fill("Table 1 field 3 destination field", "postcode");

    fireEvent.click(screen.getByRole("button", { name: "Add ref to table 1" }));
    fill("Table 1 ref 1 foreign key column", "district_id");
    fill("Table 1 ref 1 source column", "district_code");
    fill("Table 1 ref 1 parent table", "district");
    fill("Table 1 ref 1 parent key", "code");

    const json = await screen.findByTestId("mapping-json");
    expect(JSON.parse(json.textContent!)).toEqual({
      tables: {
        subdistrict_src: {
          target: "subdistrict",
          columns: { code: "code", name: "name", postcode: "postcode" },
          refs: [{ column: "district_id", parent: "master.district", parent_key: "code",
                   source_column: "district_code", external: true }],
        },
      },
    });
  });

  it("emits a new-table destination without refs (columns are target -> source)", async () => {
    render(<ScriptsTab canEdit />);

    fill("Table 1 source table", "province_src");
    fill("Table 1 new table name", "province");
    fill("Table 1 field 1 source column", "province_id");
    fill("Table 1 field 1 destination field", "code");
    fireEvent.click(screen.getByRole("button", { name: "Add field to table 1" }));
    fill("Table 1 field 2 source column", "province_name");
    fill("Table 1 field 2 destination field", "name");

    const json = await screen.findByTestId("mapping-json");
    const parsed = JSON.parse(json.textContent!);
    expect(parsed).toEqual({
      tables: { province_src: { target: "province", columns: { code: "province_id", name: "province_name" } } },
    });
    expect(parsed.tables.province_src.refs).toBeUndefined();
  });

  it("says what is wrong instead of sending a bad mapping", async () => {
    render(<ScriptsTab canEdit />);

    fill("Table 1 source table", "district_src");
    fill("Table 1 new table name", "Master.District");

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/snake_case/i);
    expect(screen.queryByTestId("mapping-json")).not.toBeInTheDocument();
  });

  it("keeps the raw mapping JSON behind the advanced toggle", async () => {
    render(<ScriptsTab canEdit />);

    fill("Table 1 source table", "province_src");
    fill("Table 1 new table name", "province");
    fill("Table 1 field 1 source column", "province_id");
    fill("Table 1 field 1 destination field", "code");

    fireEvent.click(screen.getByText(/Advanced/));
    const raw = await screen.findByLabelText("Mapping JSON (advanced)");
    expect((raw as HTMLTextAreaElement).value).toContain('"target": "province"');
  });
});
