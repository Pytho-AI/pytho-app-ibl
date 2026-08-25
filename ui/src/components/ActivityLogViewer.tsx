import { useEffect, useState, useCallback } from "react";
import { Table } from "../subframe/components/Table";
import { Button } from "../subframe/components/Button";
import { IconButton } from "../subframe/components/IconButton";
import { useAppContext } from "../lib/contextLib";

interface EventLogEntry {
  user_id: string;
  tag: string;
  timestamp: string;
  details: string;
}

export default function ActivityLogViewer({
  userId,
  tag,
  pageSize = 10,
  height = 500,
}: {
  userId: string;
  tag: string;
  pageSize?: number;
  height?: number;
}) {
  const { projectMap } = useAppContext();
  const [events, setEvents] = useState<EventLogEntry[]>([]);
  const [currentPageIndex, setCurrentPageIndex] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(false);
  const [totalPages, setTotalPages] = useState(1);

  const fetchEvents = useCallback(
    async (page: number) => {
      if (loading) return;
      setLoading(true);
      try {
        const offset = page * pageSize;
        const res = await fetch(
          `/api/event_log/${userId}?limit=${pageSize}&offset=${offset}` +
            (tag ? `&tag=${encodeURIComponent(tag)}` : "")
        );
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        setEvents(data);

        if (data.length === pageSize) {
          setHasMore(true);
          setTotalPages((prevTotal) => {
            if (page + 1 >= prevTotal) return page + 2;
            return prevTotal;
          });
        } else {
          setHasMore(false);
          setTotalPages(page + 1);
        }
      } catch (err) {
        console.error("Failed to fetch events:", err);
      } finally {
        setLoading(false);
      }
    },
    [userId, tag, pageSize, loading]
  );

  useEffect(() => {
    setEvents([]);
    setCurrentPageIndex(0);
    setHasMore(true);
    setTotalPages(1);
    setLoading(false);
  }, [userId, tag]);

  useEffect(() => {
    fetchEvents(currentPageIndex);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentPageIndex, userId, tag]);

  const isEmpty = !loading && !hasMore && events.length === 0;

  return (
    <div
      className="flex w-full flex-col rounded-md border border-solid border-neutral-border bg-default-background shadow-sm overflow-hidden"
      style={{ height: "auto" }}
    >
      <div className="flex items-center justify-between border-b border-solid border-neutral-border px-4 py-4 shrink-0">
        <span className="text-heading-3 font-heading-3 text-default-font">
          Recent Activity
        </span>
      </div>

      <div
        className={
          isEmpty ? "w-full px-2 py-8" : "flex-1 w-full overflow-y-auto px-2"
        }
      >
        {isEmpty ? (
          <div className="flex items-center justify-center py-8">
            <span className="text-body font-body text-subtext-color">
              No Recent Activity
            </span>
          </div>
        ) : (
          <>
            <Table
              header={
                <Table.HeaderRow>
                  <Table.HeaderCell>Action</Table.HeaderCell>
                  <Table.HeaderCell>Time</Table.HeaderCell>
                </Table.HeaderRow>
              }
            >
              {events.map((event, i) => {
                let displayText = event.details || event.tag;
                if (tag === "" && event.tag.startsWith("event:")) {
                  const projectId = event.tag.replace("event:", "");
                  const eventName =
                    projectMap?.[projectId]?.projectDetails?.name;
                  if (eventName) {
                    displayText = `[${eventName}] ${displayText}`;
                  }
                }
                return (
                  <Table.Row key={i}>
                    <Table.Cell>
                      <span className="whitespace-nowrap text-body font-body text-default-font">
                        {displayText}
                      </span>
                    </Table.Cell>
                    <Table.Cell>
                      <span className="whitespace-nowrap text-body font-body text-subtext-color">
                        {new Date(event.timestamp + "Z").toLocaleString()}
                      </span>
                    </Table.Cell>
                  </Table.Row>
                );
              })}
            </Table>

            {events.length > 0 && (
              <div className="flex w-full items-center justify-center gap-1 px-4 py-4">
                <div className="flex items-center justify-center gap-1">
                  <IconButton
                    disabled={currentPageIndex === 0 || loading}
                    size="medium"
                    icon="FeatherChevronFirst"
                    onClick={() => setCurrentPageIndex(0)}
                  />
                  <IconButton
                    disabled={currentPageIndex === 0 || loading}
                    size="medium"
                    icon="FeatherChevronLeft"
                    onClick={() =>
                      setCurrentPageIndex(currentPageIndex - 1)
                    }
                  />
                </div>
                <div className="flex items-center justify-center gap-1">
                  {[...Array(totalPages).keys()].map((p) => (
                    <Button
                      key={p}
                      variant={
                        p === currentPageIndex
                          ? "brand-secondary"
                          : "neutral-tertiary"
                      }
                      size="medium"
                      onClick={() => setCurrentPageIndex(p)}
                      disabled={loading}
                    >
                      {p + 1}
                    </Button>
                  ))}
                </div>
                <div className="flex items-center justify-center gap-1">
                  <IconButton
                    disabled={!hasMore || loading}
                    size="medium"
                    icon="FeatherChevronRight"
                    onClick={() =>
                      setCurrentPageIndex(currentPageIndex + 1)
                    }
                  />
                  {hasMore && (
                    <IconButton
                      disabled={loading}
                      size="medium"
                      icon="FeatherChevronLast"
                      onClick={() =>
                        setCurrentPageIndex(currentPageIndex + 1)
                      }
                    />
                  )}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
