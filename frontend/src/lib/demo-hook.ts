/**
 * Krok för demobygget (NEXT_PUBLIC_DRIVELOG_DEMO=1, `npm run build:demo`).
 *
 * I demot besvaras alla anrop mot /api/v1 av en simulerad server i webbläsaren
 * (src/demo/), som laddas dynamiskt. I det vanliga bygget är flaggan inte satt: allt här
 * är `null`/`false`, villkoren viks bort vid bygget och demokoden följer inte med.
 */

export const DEMO_MODE = process.env.NEXT_PUBLIC_DRIVELOG_DEMO === "1";

type DemoServer = typeof import("@/demo/server");

const loadDemo: (() => Promise<DemoServer>) | null =
  process.env.NEXT_PUBLIC_DRIVELOG_DEMO === "1"
    ? () =>
        import("@/demo/server").then((m) => {
          m.installFetchGuard();
          return m;
        })
    : null;

/** Ersätter fetch för anrop mot /api/v1 i demot. */
export const demoFetch: ((url: string, init: RequestInit) => Promise<Response>) | null = loadDemo
  ? (url, init) => loadDemo().then((m) => m.handleFetch(url, init))
  : null;

/** Exportfilen i demot: en riktig fil som backenden skapat ur demots påhittade resor. */
export const DEMO_EXPORT_FILE: string | null = process.env.NEXT_PUBLIC_DRIVELOG_DEMO === "1" ? "/demo/korjournal-exempel.xlsx" : null;

/**
 * Demots klocka: sidan visar demots påhittade "nu" (startdatat spelades in då) och går sedan
 * framåt i vanlig takt. Körs i <head> före allt annat. "Återställ demot" börjar om.
 */
export const DEMO_CLOCK_SCRIPT: string | null =
  process.env.NEXT_PUBLIC_DRIVELOG_DEMO === "1"
    ? `(function(){try{var R=Date,K='drivelog.demo.clock',S=R.parse('2026-11-16T08:31:00Z'),s=0;try{s=Number(localStorage.getItem(K))||0}catch(e){}var r=R.now();if(!(s>0)){s=r;try{localStorage.setItem(K,String(r))}catch(e){}}var o=S-s;function D(){var a=arguments;if(!(this instanceof D))return new R(R.now()+o).toString();var d=a.length===0?new R(R.now()+o):new(Function.prototype.bind.apply(R,[null].concat([].slice.call(a))))();Object.setPrototypeOf(d,Object.getPrototypeOf(this));return d}D.prototype=R.prototype;D.now=function(){return R.now()+o};D.parse=R.parse;D.UTC=R.UTC;window.Date=D}catch(e){}})();`
    : null;
