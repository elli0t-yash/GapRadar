# Structured Output Samples

This document summarizes representative data shapes used by GapRadar.

## 1) Discovery record shape (Fix My Itch)

```json
{
  "problem": "Why do micro-SMEs waste 10+ hours weekly on invoice management?",
  "itch_score": 67.5,
  "industry": "B2B Services",
  "description": "...",
  "severity_score": 7,
  "tam_score": 7,
  "whitespace_score": 6,
  "frequency_score": 9,
  "source": "fix_my_itch",
  "source_url": "https://razorpay.com/m/fix-my-itch/"
}
```

## 2) Research record shape (arXiv)

```json
{
  "arxiv_id": "2608.13083",
  "title": "AoI-Guaranteed Dynamic Route Planning for Connected Vehicles",
  "abstract": "...",
  "authors": ["..."],
  "published_at": "2026-08-13",
  "categories": ["Systems and Control (eess.SY)"],
  "paper_url": "https://arxiv.org/abs/2608.13083",
  "pdf_url": "https://arxiv.org/pdf/2608.13083",
  "query": "dynamic vehicle routing"
}
```

## 3) SERP organic payload consumed and normalized

Provider-side organic shape:

```json
{
  "organic": [
    {
      "title": "AI-Forecasting Software",
      "link": "https://www.crunchtime.com/restaurant-forecasting",
      "description": "Forecast demand and cut waste.",
      "global_rank": 1
    }
  ]
}
```

GapRadar normalized shape:

```json
{
  "query": "restaurant demand forecasting software",
  "title": "AI-Forecasting Software",
  "url": "https://www.crunchtime.com/restaurant-forecasting",
  "domain": "crunchtime.com",
  "snippet": "Forecast demand and cut waste.",
  "position": 1
}
```

## Notes

- Exact persisted schemas are defined in backend models and schemas.
- These examples are intentionally concise for onboarding and review contexts.
