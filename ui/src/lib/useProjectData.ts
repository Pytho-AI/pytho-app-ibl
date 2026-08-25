import { useEffect, useState, useRef } from 'react';
import { useAppContext } from './contextLib';

export function useProjectData(projectId: string | undefined) {
  const { user, projectMap, setProjectMap } = useAppContext();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const hasLoadedRef = useRef<Set<string>>(new Set());

  useEffect(() => {
    async function loadProjects() {
      // Skip if no projectId, default projectId, or no user
      if (!projectId || projectId === 'default' || !user?.userId) {
        return;
      }

      // Check if project already exists in projectMap
      if (projectMap[projectId]?.projectDetails) {
        return;
      }

      // Check if we've already attempted to load for this projectId
      if (hasLoadedRef.current.has(projectId)) {
        return;
      }

      setLoading(true);
      setError(null);
      hasLoadedRef.current.add(projectId);

      try {
        const response = await fetch(`/api/projects/${user.userId}`);

        if (response.ok) {
          const projects = await response.json();

          setProjectMap((currentMap: any) => {
            const updatedMap: any = { ...currentMap };

            for (const projectStr of projects) {
              const project = typeof projectStr === 'string' ? JSON.parse(projectStr) : projectStr;
              if (!project.isDeleted) {
                const pid = project.projectId;
                updatedMap[pid] = {
                  ...updatedMap[pid],
                  projectDetails: project,
                };
              }
            }

            // Preserve default entry
            if (currentMap?.default) {
              updatedMap.default = currentMap.default;
            }

            return updatedMap;
          });
        } else {
          const errorData = await response.json();
          console.error("Failed to fetch projects:", errorData);
          setError("Failed to load projects");
          hasLoadedRef.current.delete(projectId);
        }
      } catch (err) {
        console.error("Error loading projects:", err);
        setError(err instanceof Error ? err.message : "Failed to load projects");
        hasLoadedRef.current.delete(projectId);
      } finally {
        setLoading(false);
      }
    }

    loadProjects();
  }, [projectId, user?.userId, setProjectMap]);

  return { loading, error };
}
