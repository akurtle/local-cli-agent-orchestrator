const form = document.querySelector("#project-form");
const projects = document.querySelector("#projects");
const empty = document.querySelector("#empty");

function projectCard(project) {
  const article = document.createElement("article");
  const title = document.createElement("h3");
  const description = document.createElement("p");
  title.textContent = project.name;
  description.textContent = project.description || "No description";
  article.append(title, description);
  return article;
}

async function loadProjects() {
  const response = await fetch("/api/projects");
  const data = await response.json();
  projects.replaceChildren(...data.map(projectCard));
  empty.hidden = data.length > 0;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const response = await fetch("/api/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      name: document.querySelector("#name").value,
      description: document.querySelector("#description").value,
    }),
  });
  if (response.ok) {
    form.reset();
    await loadProjects();
  }
});

loadProjects();
