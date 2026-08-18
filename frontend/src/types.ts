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
