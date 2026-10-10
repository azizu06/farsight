import { describe, expect, it } from "vitest";
import { MjpegParser, multipartBoundary } from "./stream";

const encode = (text: string) => new TextEncoder().encode(text);
const jpeg = new Uint8Array([255, 216, 7, 8, 255, 217]);
function part(length = true) {
  return new Uint8Array([
    ...encode(
      `--camera\r\nContent-Type: image/jpeg\r\n${length ? `Content-Length: ${jpeg.length}\r\n` : ""}\r\n`,
    ),
    ...jpeg,
    ...encode("\r\n"),
  ]);
}

describe("MJPEG parser", () => {
  it("parses quoted and unquoted multipart boundaries", () => {
    expect(
      multipartBoundary('multipart/x-mixed-replace; boundary="camera"'),
    ).toBe("camera");
    expect(multipartBoundary("multipart/x-mixed-replace;boundary=camera")).toBe(
      "camera",
    );
    expect(() => multipartBoundary("image/jpeg")).toThrow();
    expect(() =>
      multipartBoundary(
        "multipart/x-mixed-replace; boundary=" + "a".repeat(100),
      ),
    ).toThrow();
  });
  it("handles every header and JPEG split without losing frames", () => {
    const parser = new MjpegParser("camera");
    const output: Uint8Array[] = [];
    for (const byte of part())
      output.push(...parser.push(new Uint8Array([byte])));
    expect(output).toEqual([jpeg]);
  });
  it("handles several frames in one network read", () => {
    const parser = new MjpegParser("camera");
    expect(parser.push(new Uint8Array([...part(), ...part()]))).toEqual([
      jpeg,
      jpeg,
    ]);
  });
  it("supports a server without Content-Length", () => {
    const parser = new MjpegParser("camera");
    expect(
      parser.push(new Uint8Array([...part(false), ...encode("--camera\r\n")])),
    ).toEqual([jpeg]);
  });
  it("rejects invalid, oversized, or mismatched JPEG parts", () => {
    expect(() => new MjpegParser("camera", 5).push(part())).toThrow(
      /large|limit/i,
    );
    expect(() =>
      new MjpegParser("camera").push(
        encode("--camera\r\nContent-Length: -1\r\n\r\n"),
      ),
    ).toThrow(/length/i);
    expect(() =>
      new MjpegParser("camera").push(
        encode("--camera\r\nContent-Length: 6\r\n\r\nbroken"),
      ),
    ).toThrow(/JPEG/i);
  });
  it("bounds unterminated headers and junk preambles", () => {
    expect(() =>
      new MjpegParser("camera").push(encode("--camera\r\n" + "x".repeat(8193))),
    ).toThrow();
    expect(() =>
      new MjpegParser("camera").push(encode("x".repeat(8193))),
    ).toThrow();
  });
});
