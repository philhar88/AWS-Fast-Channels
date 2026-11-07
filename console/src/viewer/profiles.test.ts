import { describe, expect, it } from "vitest";
import type { Persona } from "./config";
import { kidsProfileOf, signInEmail } from "./profiles";

const PERSONAS: readonly Persona[] = [
  { id: "dana", name: "Dana", bio: "", email: "dana@personas.example.com", signIn: "user", genres: [] },
  { id: "marcus", name: "Marcus", bio: "", email: "marcus@personas.example.com", signIn: "user", genres: [] },
  { id: "mia", name: "Mia", bio: "", email: null, signIn: "kids_profile", account: "marcus", genres: [] },
  { id: "guest", name: "Guest", bio: "", email: null, signIn: "guest", genres: [] },
];

const persona = (id: string): Persona => {
  const found = PERSONAS.find((p) => p.id === id);
  if (!found) {
    throw new Error(id);
  }
  return found;
};

describe("kids profiles", () => {
  it("sign in with their account's email", () => {
    expect(signInEmail(persona("mia"), PERSONAS)).toBe("marcus@personas.example.com");
    expect(signInEmail(persona("dana"), PERSONAS)).toBe("dana@personas.example.com");
    expect(signInEmail(persona("guest"), PERSONAS)).toBeNull();
  });

  it("are the persona only on their own account", () => {
    expect(kidsProfileOf("mia", persona("marcus"), PERSONAS)?.id).toBe("mia");
    expect(kidsProfileOf("mia", persona("dana"), PERSONAS)).toBeUndefined();
    expect(kidsProfileOf("dana", persona("marcus"), PERSONAS)).toBeUndefined();
    expect(kidsProfileOf(null, persona("marcus"), PERSONAS)).toBeUndefined();
  });
});
