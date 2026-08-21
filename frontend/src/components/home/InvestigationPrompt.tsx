import { useState } from "react";
import type { FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { Reveal } from "./Reveal";

/**
 * The landing page's investigation entry point.
 *
 * It DOES NOT create anything. Submitting hands the typed hypothesis to
 * /investigate as a query parameter and that page -- the authoritative one --
 * decides what to do with it. There is deliberately no second creation
 * lifecycle here, and nothing on this page can start a run.
 */
export function InvestigationPrompt() {
  const navigate = useNavigate();
  const [hypothesis, setHypothesis] = useState("");

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmed = hypothesis.trim();
    navigate(
      trimmed ? `/investigate?q=${encodeURIComponent(trimmed)}` : "/investigate",
    );
  }

  return (
    <section className="home-section home-prompt" aria-labelledby="home-prompt-title">
      <div className="container">
        <Reveal className="home-section-head">
          <p className="home-eyebrow">Investigation Mode</p>
          <h2 id="home-prompt-title" className="home-section-title">
            Already have an idea?
            <span> Make GapRadar investigate it.</span>
          </h2>
          <p className="home-lede">
            Discovery finds opportunities for you. Investigation goes the other
            way: you state a hypothesis, and GapRadar looks for academic
            research, demand evidence, and competitor candidates behind it.
          </p>
        </Reveal>

        <Reveal className="prompt-composition" delay={0.06}>
          <form className="prompt-form" onSubmit={handleSubmit}>
            <label htmlFor="home-hypothesis" className="prompt-label">
              Your hypothesis
            </label>
            <div className="prompt-field">
              <input
                id="home-hypothesis"
                type="text"
                value={hypothesis}
                onChange={(event) => setHypothesis(event.target.value)}
                placeholder="AI compliance assistant for small Indian exporters"
                autoComplete="off"
                enterKeyHint="go"
              />
              <button type="submit">
                Investigate <span aria-hidden="true">→</span>
              </button>
            </div>
            <p className="prompt-note">
              Opens the investigation workspace with your text filled in.
              Nothing is created, and no provider is called, until you ask for
              it there.
            </p>
          </form>
        </Reveal>
      </div>
    </section>
  );
}
