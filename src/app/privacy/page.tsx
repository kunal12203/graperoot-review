import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";

const SECTIONS = [
  {
    title: "Information we collect",
    body: [
      "When you install the GrapeRoot Review GitHub App, we receive access to pull request metadata — file diffs, commit messages, and repository names — strictly to perform code review.",
      "When you authenticate via GitHub OAuth, we store your GitHub user ID, username, and email address to associate reviews with your account.",
      "We log webhook payloads from GitHub to diagnose delivery failures. Logs are retained for 30 days.",
    ],
  },
  {
    title: "How we use your information",
    body: [
      "Code diffs are sent to the Anthropic API for AI analysis and are not stored beyond the duration of a single review job.",
      "Your GitHub identity is used solely to gate access to your dashboard and to attribute reviews to your repositories.",
      "We do not sell, rent, or share your data with third parties for marketing purposes.",
    ],
  },
  {
    title: "Third-party services",
    body: [
      "GitHub — repository access and OAuth authentication.",
      "Anthropic — large-language model inference for code review. Diffs are processed transiently; Anthropic's data retention policy applies.",
      "Neon — PostgreSQL database hosting for review records and user accounts.",
      "Railway — compute hosting for the backend service.",
    ],
  },
  {
    title: "Data retention",
    body: [
      "Review findings are stored until you uninstall the GitHub App or request deletion.",
      "You may request full deletion of your data by emailing hello@graperoot.dev. We will action it within 14 days.",
    ],
  },
  {
    title: "Security",
    body: [
      "All data in transit is encrypted via TLS. Database connections use SSL. GitHub webhook payloads are verified using HMAC-SHA256 before processing.",
      "Private keys and secrets are stored as environment variables and are never committed to source control.",
    ],
  },
  {
    title: "Changes to this policy",
    body: [
      "We may update this policy as the product evolves. Material changes will be announced via the GitHub App listing. Continued use after changes constitutes acceptance.",
    ],
  },
  {
    title: "Contact",
    body: [
      "Questions or deletion requests: hello@graperoot.dev",
    ],
  },
];

export default function PrivacyPage() {
  return (
    <>
      <Navbar />
      <main className="min-h-screen pt-24 pb-20 px-4 sm:px-6">
        <div className="max-w-2xl mx-auto">
          <p className="text-xs font-semibold text-grape-400 uppercase tracking-widest mb-3">Legal</p>
          <h1 className="text-3xl sm:text-4xl font-bold text-white tracking-tight mb-3">Privacy Policy</h1>
          <p className="text-zinc-400 text-sm mb-2">Effective date: 18 May 2026</p>
          <p className="text-zinc-400 text-sm mb-12 leading-relaxed">
            GrapeRoot Review (&ldquo;we&rdquo;, &ldquo;our&rdquo;) is a GitHub App that posts AI-generated code review
            comments on pull requests. This policy describes how we collect, use, and protect your information.
          </p>

          <div className="flex flex-col gap-10">
            {SECTIONS.map(({ title, body }) => (
              <section key={title}>
                <h2 className="text-base font-semibold text-white mb-3">{title}</h2>
                <div className="flex flex-col gap-2">
                  {body.map((para, i) => (
                    <p key={i} className="text-sm text-zinc-400 leading-relaxed">{para}</p>
                  ))}
                </div>
              </section>
            ))}
          </div>
        </div>
      </main>
      <Footer />
    </>
  );
}
