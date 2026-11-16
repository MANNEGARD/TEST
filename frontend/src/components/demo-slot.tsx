import dynamic from "next/dynamic";

/** Demots märkning (bara i demobygget; i det vanliga bygget är den null och följer inte med). */
export const DemoBannerSlot = process.env.NEXT_PUBLIC_DRIVELOG_DEMO === "1" ? dynamic(() => import("@/demo/banner")) : null;
