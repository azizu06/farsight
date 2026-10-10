const view = new URLSearchParams(location.search).get("view");
if (view === "vr") {
  document.title = "Farsight · Rescue / VR";
  void import("./rescueRover");
} else {
  void import("./recon");
}
export {};
