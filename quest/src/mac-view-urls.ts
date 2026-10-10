/** Join a path onto an http(s) origin and return the matching ws(s) URL. */
export function socketUrl(origin: string, path: string): string {
  return new URL(path, origin).href.replace(/^http/, "ws");
}

/** A path joins the page. A full ws:// or http:// URL is used as-is. */
export function videoSocketUrl(streamUrl: string, pageHref: string): string {
  if (/^wss?:/i.test(streamUrl)) return streamUrl;
  return new URL(streamUrl, pageHref).href.replace(/^http/, "ws");
}
