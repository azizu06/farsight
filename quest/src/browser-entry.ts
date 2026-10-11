// The desktop Mac preview has no rover controls or geometry downloads.
// The operator/Quest page and explicit local map keep their existing entry.
const query = new URLSearchParams(location.search);
if (query.get("view") === "map" && query.get("local") !== "1")
  void import("./desktop-mac");
else void import("./main");
export {};
