import { helper } from "./helper";

export class Client {
  run(): number {
    return helper(3);
  }
}

export function retry(times: number): number {
  return helper(times);
}
