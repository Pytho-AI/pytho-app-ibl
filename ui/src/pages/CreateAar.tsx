"use client";

import React, { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Breadcrumbs } from "../subframe/components/Breadcrumbs";
import { Button } from "../subframe/components/Button";
import { Tabs } from "../subframe/components/Tabs";
import { TextArea } from "../subframe/components/TextArea";
import { TextField } from "../subframe/components/TextField";
import { DefaultPageLayout } from "../subframe/layouts/DefaultPageLayout";
import { useAppContext } from "../lib/contextLib";

interface AARFormData {
  name: string;
  description: string;
}

function CreateAar() {
  const { projectId } = useAppContext();
  console.log("projectID ", projectId)
  const navigate = useNavigate();

  
  const [formData, setFormData] = useState<AARFormData>({
    name: "",
    description: "",
  });
  
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleInputChange = (
    field: keyof AARFormData,
    value: string
  ) => {
    setFormData((prev) => ({
      ...prev,
      [field]: value,
    }));
    // Clear error when user starts typing
    if (error) setError(null);
  };

  const validateForm = (): boolean => {
    if (!formData.name.trim()) {
      setError("Lesson learned name is required");
      return false;
    }
    if (!projectId) {
      setError("Project ID is required");
      return false;
    }
    return true;
  };

  const handleSubmit = async () => {
    if (!validateForm()) return;

    setIsSubmitting(true);
    setError(null);

    try {
      const response = await fetch("/api/aar", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          name: formData.name,
          description: formData.description || null,
          event_id: projectId,
        }),
      });

      if (!response.ok) {
        const errorData = await response.json();
        throw new Error(errorData.detail || "Failed to create lesson learned");
      }

      // const data = await response.json();
      
      // Redirect to the Lesson Learned Dashboard or Event Dashboard
      navigate(projectId ? `/missioncommand/events/${projectId}/aar` : `/missioncommand/events/dashboard`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "An error occurred");
    } finally {
      setIsSubmitting(false);
    }
  };

  const handleCancel = () => {
    navigate(-1); // Go back to previous page
  };

  return (
    <DefaultPageLayout>
      <div className="container max-w-none flex h-full w-full flex-col items-start gap-8 bg-default-background py-12">
        <div className="flex w-full flex-col items-start gap-2">
          <Breadcrumbs />
          <span className="text-heading-2 font-heading-2 text-default-font">
            Create Lesson Learned
          </span>
        </div>
        <div className="flex w-full items-center justify-between">
          <Tabs>
            <Tabs.Item active={true}>Lesson Learned Details</Tabs.Item>
          </Tabs>
        </div>
        <div className="flex w-full flex-col items-start gap-6">
          <div className="flex w-full flex-col items-start gap-6">
            <div className="flex w-full flex-col items-start gap-6 rounded-md border border-solid border-neutral-border bg-default-background px-6 pt-4 pb-6 shadow-sm">
              <div className="flex w-full flex-col items-start">
                <span className="w-full text-heading-3 font-heading-3 text-default-font" />
                <span className="w-full text-body font-body text-subtext-color" />
              </div>
              <div className="flex w-full grow shrink-0 basis-0 flex-col items-start gap-6">
                {error && (
                  <div className="w-full rounded-md bg-red-50 border border-red-200 px-4 py-3">
                    <p className="text-sm text-red-800">{error}</p>
                  </div>
                )}
                
                <div className="flex w-full flex-col items-start gap-4">
                  <TextField
                    className="h-auto w-full flex-none"
                    label="Lesson Learned Name"
                    helpText=""
                  >
                    <TextField.Input
                      placeholder="Enter a lesson learned name"
                      value={formData.name}
                      onChange={(event: React.ChangeEvent<HTMLInputElement>) =>
                        handleInputChange("name", event.target.value)
                      }
                      disabled={isSubmitting}
                    />
                  </TextField>
                </div>
                
                <div className="flex w-full items-start gap-4">
                  <div className="flex grow shrink-0 basis-0 flex-col items-start gap-4">
                    <TextArea
                      className="h-auto min-h-[96px] w-full flex-none"
                      label="Description"
                      helpText=""
                    >
                      <TextArea.Input
                        placeholder="Briefly describe your event. Used for internal purposes only."
                        value={formData.description}
                        onChange={(
                          event: React.ChangeEvent<HTMLTextAreaElement>
                        ) => handleInputChange("description", event.target.value)}
                        disabled={isSubmitting}
                      />
                    </TextArea>
                  </div>
                </div>
              </div>
              
              <div className="flex w-full items-center justify-end gap-2">
                <Button
                  variant="brand-secondary"
                  onClick={handleCancel}
                  disabled={isSubmitting}
                >
                  Cancel
                </Button>
                <Button
                  onClick={handleSubmit}
                  disabled={isSubmitting || !formData.name.trim()}
                >
                  {isSubmitting ? "Creating..." : "Create"}
                </Button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </DefaultPageLayout>
  );
}

export default CreateAar;