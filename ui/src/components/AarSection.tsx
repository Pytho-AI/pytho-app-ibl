import React, { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { Button } from "../subframe/components/Button";
import { IconButton } from "../subframe/components/IconButton";
import { Table } from "../subframe/components/Table";
import { useAppContext } from "../lib/contextLib";
import * as SubframeCore from "@subframe/core";
import AddObservationsModal from "./AddObservationsModal";
import CreateRecordingModal from "./CreateRecordingModal";
import DeleteAar from "./DeleteAar";

interface AAR {
  id: number;
  name: string;
  description: string | null;
  event_id: string | null;
  created_at: string;
  updated_at: string;
}

function AARSection() {
  const nav = useNavigate();
  const { projectId } = useAppContext();
  const [aars, setAars] = useState<AAR[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showAddObservationsModal, setShowAddObservationsModal] = useState(false);
  const [showCreateRecordingModal, setShowCreateRecordingModal] = useState(false);
  const [openConfirmDeleteAar, setOpenConfirmDeleteAar] = useState(false);
  const [deleteAar, setDeleteAar] = useState<AAR | null>(null);
  const [showEditObservationsModal, setShowEditObservationsModal] = useState(false);
  const [editAar, setEditAar] = useState<AAR | null>(null);
  const [currentPage, setCurrentPage] = useState(1);
  const itemsPerPage = 5;

  const isTextObservation = (aar: AAR) => !!aar.description?.trim();

  useEffect(() => {
    if (projectId) {
      fetchAARs();
    }
  }, [projectId]);

  useEffect(() => {
    setCurrentPage(1);
  }, [aars.length]);

  const fetchAARs = async () => {
    try {
      setLoading(true);
      const response = await fetch(`/api/aar?event_id=${projectId}`);
      if (!response.ok) {
        throw new Error("Failed to fetch lesson learned inputs");
      }
      const data = await response.json();
      setAars(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "An error occurred");
    } finally {
      setLoading(false);
    }
  };

  const handleAARClick = (aarId: number, aarName: string) => {
    nav(`/missioncommand/events/aar/${aarId}`, {
      state: { aarName, projectId, fromLessonLearnedDashboard: false },
    });
  };

  const handleViewAll = () => {
    if (projectId && projectId !== "default") {
      nav(`/missioncommand/events/${projectId}/aar`);
    }
  };

  const handleRecordHotwash = () => {
    setShowCreateRecordingModal(true);
  };

  const handleAddObservations = () => {
    setShowAddObservationsModal(true);
  };

  const totalPages = Math.ceil(aars.length / itemsPerPage);
  const startIndex = (currentPage - 1) * itemsPerPage;
  const endIndex = startIndex + itemsPerPage;
  const paginatedAars = aars.slice(startIndex, endIndex);

  return (
    <div className="flex w-full flex-col items-start gap-4">
      <DeleteAar
        open={openConfirmDeleteAar}
        setOpen={setOpenConfirmDeleteAar}
        aar={deleteAar}
        onDeleteSuccess={fetchAARs}
      />
      <AddObservationsModal
        open={showAddObservationsModal}
        onOpenChange={setShowAddObservationsModal}
        projectId={projectId}
        onSuccess={fetchAARs}
      />
      <AddObservationsModal
        open={showEditObservationsModal}
        onOpenChange={(open) => {
          setShowEditObservationsModal(open);
          if (!open) setEditAar(null);
        }}
        projectId={projectId}
        onSuccess={fetchAARs}
        editMode={true}
        existingAar={editAar ? { id: editAar.id, name: editAar.name, description: editAar.description } : undefined}
      />
      <CreateRecordingModal
        open={showCreateRecordingModal}
        onOpenChange={setShowCreateRecordingModal}
        projectId={projectId}
      />
      <div className="flex w-full flex-col items-start gap-4 rounded-md border border-solid border-neutral-border bg-default-background px-4 py-4">
        <div className="flex w-full items-center justify-between">
          <span className="text-heading-3 font-heading-3 text-default-font">
            Lesson Learned Inputs
          </span>
          <div className="flex items-center gap-2">
            <Button
              variant="neutral-secondary"
              size="small"
              icon="FeatherFileText"
              onClick={handleAddObservations}
            >
              Add Observations
            </Button>
            <Button
              variant="brand-primary"
              size="small"
              icon="FeatherMic"
              onClick={handleRecordHotwash}
            >
              Record Hotwash
            </Button>
            {projectId && projectId !== "default" && (
              <Button
                variant="neutral-secondary"
                size="small"
                icon="FeatherArrowRight"
                onClick={handleViewAll}
              >
                View All
              </Button>
            )}
          </div>
        </div>

        {loading ? (
          <div className="flex w-full items-center justify-center py-8">
            <span className="text-body font-body text-subtext-color">
              Loading lesson learned inputs...
            </span>
          </div>
        ) : error ? (
          <div className="flex w-full items-center justify-center py-8">
            <span className="text-body font-body text-red-600">{error}</span>
          </div>
        ) : aars.length === 0 ? (
          <div className="flex w-full flex-col items-center justify-center gap-4 py-8">
            <SubframeCore.Icon
              className="text-heading-2 font-heading-2 text-subtext-color"
              name="FeatherMic"
            />
            <span className="text-body font-body text-subtext-color text-center">
              No lesson learned inputs yet. Record a hotwash or add text
              observations to get started.
            </span>
          </div>
        ) : (
          <>
            <Table
              header={
                <Table.HeaderRow>
                  <Table.HeaderCell>Title</Table.HeaderCell>
                  <Table.HeaderCell>Created</Table.HeaderCell>
                  <Table.HeaderCell className="h-auto grow shrink-0 basis-0 self-stretch">
                    Actions
                  </Table.HeaderCell>
                  <Table.HeaderCell />
                </Table.HeaderRow>
              }
            >
              {paginatedAars.map((aar) => (
                <Table.Row key={aar.id}>
                  <Table.Cell>
                    <div className="flex items-center gap-2">
                      <span className="whitespace-nowrap text-body-bold font-body-bold text-default-font">
                        {aar.name}
                      </span>
                    </div>
                  </Table.Cell>
                  <Table.Cell>
                    <span className="whitespace-nowrap text-body font-body text-subtext-color">
                      {new Date(aar.created_at).toLocaleDateString()}
                    </span>
                  </Table.Cell>
                  <Table.Cell className="h-auto w-auto flex-none">
                    <div className="flex items-center justify-end gap-2">
                      {isTextObservation(aar) && (
                        <IconButton
                          size="medium"
                          icon="FeatherEdit"
                          onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                            event.stopPropagation();
                            setEditAar(aar);
                            setShowEditObservationsModal(true);
                          }}
                        />
                      )}
                      <IconButton
                        size="medium"
                        icon="FeatherTrash2"
                        onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                          event.stopPropagation();
                          setDeleteAar(aar);
                          setOpenConfirmDeleteAar(true);
                        }}
                      />
                    </div>
                  </Table.Cell>
                  <Table.Cell className="h-auto w-auto flex-none">
                    <div className="flex items-center justify-end">
                      <Button
                        size="small"
                        icon={isTextObservation(aar) ? "FeatherEdit3" : "FeatherArrowRight"}
                        onClick={(event: React.MouseEvent<HTMLButtonElement>) => {
                          event.stopPropagation();
                          if (isTextObservation(aar)) {
                            setEditAar(aar);
                            setShowEditObservationsModal(true);
                          } else {
                            handleAARClick(aar.id, aar.name);
                          }
                        }}
                      >
                        {isTextObservation(aar) ? "Edit" : "View"}
                      </Button>
                    </div>
                  </Table.Cell>
                </Table.Row>
              ))}
            </Table>
            {aars.length > itemsPerPage && (
              <div className="flex w-full items-center justify-between gap-4 px-2">
                <span className="text-body font-body text-subtext-color">
                  Showing {startIndex + 1}-{Math.min(endIndex, aars.length)} of{" "}
                  {aars.length}
                </span>
                <div className="flex items-center gap-2">
                  <Button
                    variant="neutral-secondary"
                    size="small"
                    icon="FeatherChevronLeft"
                    onClick={() =>
                      setCurrentPage((prev) => Math.max(1, prev - 1))
                    }
                    disabled={currentPage === 1}
                  >
                    Previous
                  </Button>
                  <span className="text-body font-body text-default-font">
                    Page {currentPage} of {totalPages}
                  </span>
                  <Button
                    variant="neutral-secondary"
                    size="small"
                    iconRight="FeatherChevronRight"
                    onClick={() =>
                      setCurrentPage((prev) =>
                        Math.min(totalPages, prev + 1)
                      )
                    }
                    disabled={currentPage === totalPages}
                  >
                    Next
                  </Button>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export default AARSection;
