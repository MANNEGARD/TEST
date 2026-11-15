import { formatNumber } from "@/lib/format";

/**
 * Tal som i Excel-filen (backend/app/services/export/simple.py): mil med två decimaler,
 * belopp i kronor med ören och ersättningsnivån som "25" eller "18,50".
 */
export function milText(mil: number): string {
  return `${formatNumber(mil, 2, 2)} mil`;
}

export function milNumberText(mil: number): string {
  return formatNumber(mil, 2, 2);
}

export function amountNumberText(amount: number): string {
  return formatNumber(amount, 2, 2);
}

export function amountText(amount: number): string {
  return `${amountNumberText(amount)} kr`;
}

export function rateText(rate: number): string {
  return Number.isInteger(rate) ? formatNumber(rate, 0) : formatNumber(rate, 2, 2);
}
