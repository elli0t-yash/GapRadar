# 3. Example Structured Output From Bright Data

GapRadar uses Bright Data in three places, so this submission sample includes the structured output for all three paths:

1. **Razorpay Fix My Itch** — custom Bright Data Scraper Studio collector
2. **arXiv** — custom Bright Data Scraper Studio collector
3. **Google/public web** — Bright Data SERP API

The accompanying JSON file contains all of these examples in machine-readable form.

---

## 1. Fix My Itch — Scraper Studio Output

**Collector:** `gapradar-fix-my-itch`  
**Source:** https://razorpay.com/m/fix-my-itch/

This is representative data from GapRadar's verified healthy collector output.

```json
[
  {
    "problem": "Why do freelancers ghost projects after partial payments without accountability systems?",
    "itch_score": 76,
    "industry": "B2B Services",
    "description": "Businesses hiring ad-hoc freelancers through informal channels experience high churn rates when workers disappear mid-project after receiving partial payments, leaving companies with incomplete deliverables and no legal framework to recover damages or enforce completion.",
    "severity_score": 8,
    "tam_score": 7,
    "whitespace_score": 7.5,
    "frequency_score": 7,
    "source": "fix_my_itch",
    "source_url": "https://razorpay.com/m/fix-my-itch/",
    "input": {
      "url": "https://razorpay.com/m/fix-my-itch/"
    }
  },
  {
    "problem": "Why do micro-SMEs waste 10+ hours weekly on invoice management?",
    "itch_score": 67.5,
    "industry": "B2B Services",
    "description": "Micro and small businesses waste significant administrative time creating, tracking, and reconciling invoices manually because enterprise-grade ERP systems are prohibitively expensive, overly complex for their simple needs, and require extensive training that small teams cannot afford.",
    "severity_score": 7,
    "tam_score": 7,
    "whitespace_score": 6,
    "frequency_score": 9,
    "source": "fix_my_itch",
    "source_url": "https://razorpay.com/m/fix-my-itch/",
    "input": {
      "url": "https://razorpay.com/m/fix-my-itch/"
    }
  }
]
```

Each record gives GapRadar a structured market problem with its description, industry and published scoring signals.

### Flow

```text
Razorpay Fix My Itch
        ↓
Bright Data Scraper Studio
        ↓
Structured JSON
        ↓
Source validation + RecallGuard
        ↓
Opportunity Engine
```

---

## 2. arXiv — Scraper Studio Output

**Collector:** `gapradar-arxiv-research-v1`  
**Source:** https://arxiv.org/search/

GapRadar sends dynamically generated arXiv search URLs to this collector. The collector returns structured research-paper records.

The example below comes from the repository's arXiv research fixture and matches the structure expected from the collector.

```json
[
  {
    "arxiv_id": "2608.13083",
    "title": "AoI-Guaranteed Dynamic Route Planning for Connected Vehicles",
    "abstract": "The advancement of Intelligent Transportation Sys- tems (ITS) has been significantly driven by progress in radio communication technology. Dynamic route planning, a key com- ponent of ITS, traditionally focuses on metrics such as route capacity and travel time. This paper presents a novel dual- factor approach that integrates travel time estimation and radio resource availability into an innovative route-planning scheme for connected vehicles (CVs). To address this dual-objective route planning challenge, we employ Deep Reinforcement Learning (DRL). Our approach, called AoI-Guaranteed Dynamic Route Planning (AGDRP), effectively balances travel time and Age of Information (AoI), enhancing route planning performance through adaptive learning over time. Simulation results demon- strate that AGDRP outperforms the baseline scheme, which solely focuses on travel time optimization. In fact, we show that incor- porating AoI minimization significantly enhances route planning performance beyond conventional travel-time-based approaches.",
    "authors": [
      "Sajedeh Norouzi",
      "Maryam Ansarifard",
      "Farshad Zeinali",
      "Ali Nouruzi",
      "Nader Mokari",
      "Hamid Saeedi",
      "Nizar Zorba"
    ],
    "published_at": "2026-08-13",
    "categories": [
      "Systems and Control (eess.SY)"
    ],
    "paper_url": "https://arxiv.org/abs/2608.13083",
    "pdf_url": "https://arxiv.org/pdf/2608.13083",
    "query": "dynamic vehicle routing"
  },
  {
    "arxiv_id": "2607.16875",
    "title": "A Deep Reinforcement Learning Algorithm for the Vehicle Routing Problem with Stochastic Demands and Outsourcing",
    "abstract": "We introduce the vehicle routing problem with stochastic demands and outsourcing options (VRP-SDO), in which a logistics service provider partitions customer requests into customers outsourced to a common carrier and customers committed to its fixed fleet. The latter induces a vehicle routing problem with stochastic demands (VRP-SD), solved dynamically. Demands are revealed upon visit; residual demand may be served by other vehicles or after restocking at the depot. Work beyond the regular shift incurs overtime costs, and the unit outsourcing cost decreases with the expected outsourced demand. The objective is to minimize expected travel, overtime, and outsourcing costs.",
    "authors": [
      "Mohsen Dastpak",
      "Fausto Errico",
      "Ola Jabali"
    ],
    "published_at": "2026-07-18",
    "categories": [
      "Optimization and Control (math.OC)",
      "Artificial Intelligence (cs.AI)"
    ],
    "paper_url": "https://arxiv.org/abs/2607.16875",
    "pdf_url": "https://arxiv.org/pdf/2607.16875",
    "query": "dynamic vehicle routing"
  }
]
```

Typical fields include:

- `arxiv_id`
- `title`
- `abstract`
- `authors`
- `published_at`
- `categories`
- `paper_url`
- `pdf_url`
- `query`

### Flow

```text
Investigation hypothesis
        ↓
Research query
        ↓
Dynamic arXiv search URL
        ↓
Bright Data Scraper Studio
        ↓
Structured paper records
        ↓
GapRadar Research Evidence
```

---

## 3. Bright Data SERP API — Structured Web Search Output

SERP is a separate Bright Data product rather than a Scraper Studio collector.

GapRadar requests **`parsed_light` organic results** from Bright Data's SERP API for demand evidence and competitor discovery.

### Bright Data response shape consumed by GapRadar

The production adapter expects an organic-results payload in this form:

```json
{
  "organic": [
    {
      "title": "AI-Forecasting Software",
      "link": "https://www.crunchtime.com/restaurant-forecasting",
      "description": "Forecast demand and cut waste.",
      "global_rank": 1
    },
    {
      "title": "Inventory Forecasting for Restaurants",
      "link": "https://marketman.com/forecasting?utm_source=serp",
      "description": "Predict par levels automatically.",
      "global_rank": 2
    }
  ]
}
```

### After GapRadar normalization

GapRadar converts those provider records into a stable internal structure:

```json
[
  {
    "query": "restaurant demand forecasting software",
    "title": "AI-Forecasting Software",
    "url": "https://www.crunchtime.com/restaurant-forecasting",
    "domain": "crunchtime.com",
    "snippet": "Forecast demand and cut waste.",
    "position": 1
  },
  {
    "query": "restaurant demand forecasting software",
    "title": "Inventory Forecasting for Restaurants",
    "url": "https://marketman.com/forecasting",
    "domain": "marketman.com",
    "snippet": "Predict par levels automatically.",
    "position": 2
  }
]
```

Notice that GapRadar normalizes the URL and removes tracking parameters before the evidence is used.

### Flow

```text
Investigation hypothesis
        ↓
Demand / competitor search query
        ↓
Bright Data SERP API
        ↓
Structured organic results
        ↓
GapRadar normalization
        ↓
Demand Evidence / Competitor Candidates
```

---

# Summary

| Bright Data usage | Output |
|---|---|
| **Fix My Itch Scraper Studio collector** | Structured market-problem records |
| **arXiv Scraper Studio collector** | Structured academic-paper records |
| **SERP API** | Structured organic web-search results |

Together, these outputs provide GapRadar with three complementary evidence types:

**market problems + academic research + public-web demand/competition evidence.**
