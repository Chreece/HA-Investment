const indicationsUrl = new URL(import.meta.url);
const ASSET_REVISION = String(indicationsUrl.searchParams.get("asset_revision") || "").trim();
const RUNTIME_REVISION = String(indicationsUrl.searchParams.get("runtime_revision") || "").trim();
if (!ASSET_REVISION) throw new Error("HA Investment panel revision is missing");

const contributionUrl = new URL("./investment-panel-contribution.js", indicationsUrl);
contributionUrl.searchParams.set("asset_revision", ASSET_REVISION);
if (RUNTIME_REVISION) contributionUrl.searchParams.set("runtime_revision", RUNTIME_REVISION);
await import(contributionUrl.href);

const PANEL_NAME = `investment-panel-${ASSET_REVISION.split("-").at(-1)}`;
const Panel = customElements.get(PANEL_NAME);
if (!Panel) throw new Error("HA Investment contribution panel failed to register");

const proto = Panel.prototype;
if (!proto.__investmentIndicationsPurchaseBudgetOnly) {
  Object.defineProperty(proto, "__investmentIndicationsPurchaseBudgetOnly", {value: true});

  const planningPayload = proto.indicationPlanningPayload;
  proto.indicationPlanningPayload = function(...args) {
    // The control is deliberately absent from Indications. A stale saved value
    // must neither fail client-side validation nor enter the request payload.
    return {...planningPayload.apply(this, args), existing_cash: 0};
  };
}
