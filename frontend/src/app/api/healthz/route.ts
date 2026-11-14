/** Hälsokontroll för Docker (webbtjänsten själv, inte backend). */
export const dynamic = "force-dynamic";

export function GET() {
  return Response.json({ status: "ok" }, { headers: { "Cache-Control": "no-store" } });
}
