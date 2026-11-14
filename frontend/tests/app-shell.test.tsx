// @vitest-environment jsdom
import "@testing-library/jest-dom/vitest";
import { afterEach, expect, it, vi } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { AppShell } from "../components/app-shell";

vi.mock("next/navigation", () => ({ usePathname: () => "/documents" }));
afterEach(() => cleanup());

it("marks the current page and provides a keyboard-dismissible mobile menu", async () => {
  render(<AppShell><p>Synthetic content</p></AppShell>);
  expect(screen.getByRole("link", { name: "Документы" })).toHaveAttribute("aria-current", "page");
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Открыть меню" }));
  const dialog = await screen.findByRole("dialog");
  expect(within(dialog).getByRole("link", { name: "Документы" })).toBeInTheDocument();
  await user.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});
