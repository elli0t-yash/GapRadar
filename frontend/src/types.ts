export interface Listing {
  id: string;
  title: string;
  category: string;
  score: number;
}

export interface Problem {
  id: string;
  title: string;
  description: string;
  category: string;
  source: string;
  sourceUrl: string;
  signal: number;
  date: string;
  scores: {
    itch: number;
    severity: number;
    tam: number;
    whitespace: number;
    frequency: number;
  };
}
