import { Route, Routes } from "react-router-dom";
import { GapRadarPage } from "./pages/GapRadarPage";
import { ComingSoonPage } from "./pages/ComingSoonPage";

function App() {
  return (
    <Routes>
      <Route path="/" element={<GapRadarPage />} />
      <Route
        path="/trends"
        element={
          <ComingSoonPage
            title="Trends"
            subtitle="Track how problem signals move over time."
          />
        }
      />
      <Route
        path="/saved"
        element={
          <ComingSoonPage
            title="Saved"
            subtitle="Bookmark problems you want to revisit."
          />
        }
      />
    </Routes>
  );
}

export default App;
